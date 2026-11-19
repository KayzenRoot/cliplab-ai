"use strict";

const byId = (id) => document.getElementById(id);
const jobForm = byId("job-form");
const musicForm = byId("music-form");
const clipForm = byId("clip-form");
const player = byId("video");
const loadGuard = { "X-ClipLab": "1" };
const stages = {
  CREATED: "Na fila",
  VALIDATING: "Validando gravação",
  EXTRACTING_AUDIO: "Extraindo áudio",
  TRANSCRIBING: "Transcrevendo áudio",
  ANALYZING: "Analisando a transcrição",
  SELECTING: "Selecionando trechos",
  RENDERING: "Renderizando prévias",
  VERIFYING: "Verificando arquivos",
  READY_FOR_REVIEW: "Pronto para revisão",
  FAILED: "Falha no processamento",
  CANCELLED: "Processamento cancelado",
  BLOCKED: "Processamento bloqueado",
};
const stoppedStates = new Set(["FAILED", "CANCELLED", "BLOCKED"]);
const clipStates = { pending: "Em revisão", approved: "Aprovado", discarded: "Descartado" };

let recordingFile = null;
let maxUploadBytes = 0;
let tracks = [];
let currentJobId = null;
let currentJob = null;
let currentClipId = null;
let projectRequest = 0;
let pollTimer = null;
let clipListKey = "";
let clipFormKey = "";
let clipFormDirty = false;
let clipMutation = false;
let jobMutation = false;

function jobUrl(id) {
  return "/api/jobs/" + encodeURIComponent(id);
}

function clipUrl() {
  return jobUrl(currentJobId) + "/clips/" + encodeURIComponent(currentClipId);
}

function message(payload, fallback) {
  if (typeof payload?.detail === "string") return payload.detail;
  if (Array.isArray(payload?.detail)) {
    return payload.detail.map((issue) => issue.msg).filter(Boolean).join(" · ") || fallback;
  }
  return fallback;
}

async function api(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { ...loadGuard, ...options.headers } });
  if (response.status === 204) return null;
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(message(data, "A operação falhou (HTTP " + response.status + ")."));
  return data;
}

function notice(id, text) {
  const element = byId(id);
  const content = text || "";
  if (element.textContent !== content) element.textContent = content;
  element.hidden = !content;
}

function warningList(id, warnings = []) {
  const element = byId(id);
  const items = warnings.map((warning) => {
    const node = document.createElement("li");
    node.textContent = warning;
    return node;
  });
  element.replaceChildren(...items);
  element.hidden = !items.length;
}

function actionLink(id, url) {
  const link = byId(id);
  if (url) link.href = url;
  else link.removeAttribute("href");
  link.hidden = !url;
}

function formatBytes(bytes) {
  const unit = bytes >= 1024 ** 3 ? "gigabyte" : bytes >= 1024 ** 2 ? "megabyte" : "kilobyte";
  const divisor = { gigabyte: 1024 ** 3, megabyte: 1024 ** 2, kilobyte: 1024 }[unit];
  return new Intl.NumberFormat("pt-BR", { style: "unit", unit, maximumFractionDigits: 1 }).format(bytes / divisor);
}

function timestamp(seconds) {
  const total = Math.round(seconds * 1000);
  const hours = Math.floor(total / 3600000);
  const minutes = Math.floor(total / 60000) % 60;
  const wholeSeconds = Math.floor(total / 1000) % 60;
  const clock = String(minutes).padStart(2, "0") + ":" + String(wholeSeconds).padStart(2, "0");
  const ms = String(total % 1000).padStart(3, "0");
  return (hours ? String(hours).padStart(2, "0") + ":" : "") + clock + "." + ms;
}

function setFile(file) {
  if (!file) return;
  recordingFile = null;
  byId("file-name").textContent = "";
  notice("form-error", "");
  if (!/\.(mp4|mkv|mov)$/i.test(file.name)) {
    notice("form-error", "Escolha uma gravação nos formatos MP4, MKV ou MOV.");
    return;
  }
  if (!file.size || (maxUploadBytes && file.size > maxUploadBytes)) {
    notice("form-error", !file.size ? "O arquivo selecionado está vazio." : "O arquivo supera o limite de " + formatBytes(maxUploadBytes) + ".");
    return;
  }
  recordingFile = file;
  byId("file-name").textContent = file.name + " · " + formatBytes(file.size);
}

function uploadRecording(formData) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    const progress = byId("upload-progress");
    request.open("POST", "/api/jobs");
    request.setRequestHeader("X-ClipLab", "1");
    request.responseType = "json";
    progress.removeAttribute("value");
    notice("upload-status", "Enviando gravação…");
    request.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;
      const percent = Math.round(100 * event.loaded / event.total);
      progress.value = percent;
      notice("upload-status", percent < 100 ? "Enviando gravação · " + percent + "%" : "Registrando gravação…");
    });
    request.addEventListener("load", () => {
      if (request.status >= 200 && request.status < 300 && request.response?.id) resolve(request.response.id);
      else reject(new Error(message(request.response, "Não foi possível registrar a gravação.")));
    });
    request.addEventListener("error", () => reject(new Error("A conexão foi perdida durante o envio da gravação.")));
    request.send(formData);
  });
}

const dropZone = byId("drop-zone");
byId("choose-file").addEventListener("click", () => byId("file").click());
byId("file").addEventListener("change", (event) => setFile(event.target.files[0]));
for (const eventName of ["dragenter", "dragover"]) {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    if (!byId("generate").disabled) dropZone.classList.add("dragging");
  });
}
dropZone.addEventListener("dragleave", (event) => {
  if (!dropZone.contains(event.relatedTarget)) dropZone.classList.remove("dragging");
});
dropZone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropZone.classList.remove("dragging");
  if (byId("generate").disabled) return;
  if (event.dataTransfer.files.length > 1) {
    recordingFile = null;
    byId("file-name").textContent = "";
    notice("form-error", "Envie uma gravação por vez para configurar seus cortes.");
    return;
  }
  setFile(event.dataTransfer.files[0]);
});

function updateMusicFields() {
  const mode = byId("music-mode").value;
  const select = byId("music-select");
  byId("volume-field").hidden = mode === "off";
  byId("library-field").hidden = mode !== "licensed";
  select.disabled = mode !== "licensed";
  select.required = mode === "licensed";
  const track = tracks.find((item) => item.id === select.value);
  byId("music-attribution").textContent = track ? [track.license, track.attribution].filter(Boolean).join(" · ") : "";
}

async function loadTracks(preferredId) {
  tracks = await api("/api/music");
  const select = byId("music-select");
  const previouslySelected = preferredId || select.value;
  select.replaceChildren(new Option("Selecione uma faixa", ""), ...tracks.map((track) => new Option(track.title + " — " + track.author, track.id)));
  select.value = tracks.some((item) => item.id === previouslySelected) ? previouslySelected : "";
  updateMusicFields();
}

byId("music-mode").addEventListener("change", updateMusicFields);
byId("music-select").addEventListener("change", updateMusicFields);
byId("music-volume").addEventListener("input", (event) => {
  byId("volume-value").value = Math.round(Number(event.target.value) * 100) + "%";
});

musicForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = musicForm.querySelector("button[type=submit]");
  submit.disabled = true;
  const status = byId("music-notice");
  status.className = "form-notice";
  notice("music-notice", "Salvando faixa…");
  try {
    const track = await api("/api/music", { method: "POST", body: new FormData(musicForm) });
    musicForm.reset();
    await loadTracks(track?.id);
    byId("music-mode").value = "licensed";
    updateMusicFields();
    notice("music-notice", "Faixa disponível na biblioteca licenciada.");
  } catch (error) {
    status.className = "alert";
    notice("music-notice", error.message);
  } finally {
    submit.disabled = false;
  }
});

jobForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  notice("form-error", "");
  if (!recordingFile) {
    notice("form-error", "Selecione uma gravação para gerar cortes.");
    byId("choose-file").focus();
    return;
  }
  const formData = new FormData(jobForm);
  if (Number(formData.get("minimum")) > Number(formData.get("maximum"))) {
    notice("form-error", "A duração máxima precisa ser igual ou maior que a mínima.");
    jobForm.elements.maximum.focus();
    return;
  }
  formData.set("file", recordingFile, recordingFile.name);
  byId("generate").disabled = true;
  byId("choose-file").disabled = true;
  byId("file").disabled = true;
  byId("upload-meter").hidden = false;
  try {
    const id = await uploadRecording(formData);
    recordingFile = null;
    byId("file").value = "";
    byId("file-name").textContent = "";
    await selectProject(id);
    await updateProjects(id).catch((error) => notice("connection-error", error.message));
    byId("review-heading").focus();
  } catch (error) {
    notice("form-error", error.message);
  } finally {
    byId("generate").disabled = false;
    byId("choose-file").disabled = false;
    byId("file").disabled = false;
    byId("upload-meter").hidden = true;
  }
});

function activeJob(job) {
  return job.state !== "READY_FOR_REVIEW" && !stoppedStates.has(job.state);
}

function selectedClip() {
  return currentJob?.clips.find((clip) => clip.id === currentClipId) || null;
}

function resetPreview(url) {
  if (player.getAttribute("src") === url) return;
  if (url) player.src = url;
  else {
    player.pause();
    player.removeAttribute("src");
    player.load();
  }
}

function renderClipList(clips) {
  const list = byId("clip-list");
  const key = JSON.stringify(clips.map(({ id, title, status, start, end }) => [id, title, status, start, end]));
  if (key !== clipListKey) {
    const previouslyFocused = document.activeElement?.dataset?.clip;
    clipListKey = key;
    const buttons = clips.map((clip) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "clip-tab";
      button.dataset.clip = clip.id;
      const title = document.createElement("span");
      title.textContent = clip.title;
      const duration = document.createElement("span");
      duration.className = "mono";
      duration.textContent = Math.round(clip.end - clip.start) + " s";
      const status = document.createElement("span");
      status.className = "clip-state-label";
      status.textContent = clipStates[clip.status];
      button.append(title, duration, status);
      button.addEventListener("click", () => {
        if (clip.id === currentClipId) return;
        currentClipId = clip.id;
        renderProject();
      });
      return button;
    });
    list.replaceChildren(...buttons);
    buttons.find((button) => button.dataset.clip === previouslyFocused)?.focus();
  }
  for (const button of list.children) button.setAttribute("aria-pressed", String(button.dataset.clip === currentClipId));
  list.hidden = !clips.length;
}

function renderDefinitionList(id, data) {
  const entries = data.filter(([, value]) => value).flatMap(([key, value]) => {
    const term = document.createElement("dt");
    const detail = document.createElement("dd");
    term.textContent = key;
    detail.textContent = value;
    return [term, detail];
  });
  byId(id).replaceChildren(...entries);
}

function syncClipForm(clip) {
  const key = currentJobId + "/" + clip.id;
  if (clipFormKey !== key) {
    clipFormKey = key;
    clipFormDirty = false;
    byId("clip-notice").className = "form-notice";
    notice("clip-notice", "");
  }
  if (clipFormDirty) return;
  for (const field of ["start", "end", "title", "hook", "description"]) {
    const value = String(clip[field] ?? "");
    if (clipForm.elements[field].value !== value) clipForm.elements[field].value = value;
  }
}

function updateClipActions(clip) {
  const busy = currentJob.state !== "READY_FOR_REVIEW" || clipMutation || Boolean(clip.operation);
  for (const input of clipForm.querySelectorAll("input, textarea")) input.readOnly = clipMutation;
  byId("save-clip").disabled = busy || !clipFormDirty;
  byId("approve-clip").hidden = clip.status === "approved";
  byId("approve-clip").disabled = busy || (!clip.preview_ready && clip.status !== "discarded") || clipFormDirty;
  byId("discard-clip").hidden = clip.status === "discarded";
  byId("discard-clip").disabled = busy;
  byId("retry-preview").hidden = !clip.error || clip.preview_ready || clip.status === "discarded";
  byId("retry-preview").disabled = busy || clipFormDirty;
  byId("approve-clip").textContent = clip.status === "discarded" && !clip.preview_ready ? "Restaurar para revisão" : "Aprovar corte";
  byId("export-clip").hidden = clip.status !== "approved" || clip.final_ready;
  byId("export-clip").disabled = busy || !clip.preview_ready || clipFormDirty;
}

function renderClip(clip) {
  const url = clipUrl();
  const version = encodeURIComponent(clip.revision ?? "0");
  resetPreview(clip.preview_ready ? url + "/preview?v=" + version : null);
  player.hidden = !clip.preview_ready;
  byId("empty-preview").hidden = clip.preview_ready;
  const rendering = clip.operation === "preview" || (activeJob(currentJob) && !clip.error);
  notice("preview-heading", rendering ? "Gerando prévia" : "Prévia indisponível");
  notice("preview-detail", rendering ? "O corte vertical será exibido aqui quando estiver pronto." : "Veja o estado do corte abaixo.");
  byId("monitor-duration").textContent = Math.round(clip.end - clip.start) + " s · " + timestamp(clip.start) + " na origem";
  byId("clip-details").hidden = false;
  notice("clip-title", clip.title);
  notice("clip-status", clipStates[clip.status]);
  const score = typeof clip.score === "number" ? new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 1 }).format(clip.score) : null;
  notice("clip-score", score === null ? "" : "Nota editorial estimada · " + score + " / 100");
  notice("clip-rationale", clip.rationale);
  notice("clip-error", clip.error);
  notice("clip-operation", { preview: "Renderizando prévia…", export: "Renderizando exportação…"}[clip.operation]);
  const progress = byId("clip-progress");
  progress.hidden = !clip.operation;
  if (typeof clip.operation_progress === "number") progress.value = clip.operation_progress;
  else progress.removeAttribute("value");
  warningList("clip-warnings", clip.warnings);
  renderDefinitionList("editorial-data", [
    ["Trecho original", timestamp(clip.start) + " – " + timestamp(clip.end)],
    ["Resumo", clip.summary],
    ["Hashtags", Array.isArray(clip.hashtags) ? clip.hashtags.join(" ") : clip.hashtags],
    ["Pergunta para comentários", clip.question],
  ]);
  syncClipForm(clip);
  updateClipActions(clip);
  actionLink("download-mp4", clip.final_ready && clip.status === "approved" ? url + "/mp4" : null);
  actionLink("download-srt", clip.preview_ready || clip.final_ready ? url + "/srt" : null);
}

function renderProject() {
  const job = currentJob;
  byId("job-progress").hidden = !job;
  byId("source-data").hidden = !job?.source;
  byId("project-downloads").hidden = true;
  const clips = job?.clips || [];
  if (!clips.some((clip) => clip.id === currentClipId)) currentClipId = clips[0]?.id ?? null;
  renderClipList(clips);
  const clip = selectedClip();
  if (clip) renderClip(clip);
  else {
    byId("clip-details").hidden = true;
    player.hidden = true;
    resetPreview(null);
    byId("empty-preview").hidden = false;
    const pending = job && activeJob(job);
    notice("preview-heading", pending ? "Preparando seus cortes" : job ? "Sem prévias disponíveis" : "Seus cortes aparecem aqui");
    notice("preview-detail", job ? "Acompanhe o estado do processamento acima." : "Envie uma gravação para começar.");
    byId("monitor-duration").textContent = "Formato vertical";
  }
  if (!job) return;
  if (job.source) {
    renderDefinitionList("source-metadata", [
      ["Autor", job.source.author],
      ["Origem", job.source.origin],
      ["Licença e condições", job.source.license],
      ["Referência do YouTube", job.source.youtube_url],
    ]);
  }
  notice("job-state", stages[job.state] || job.state);
  notice("job-status", job.message);
  notice("job-error", job.error);
  warningList("job-warnings", job.warnings);
  const progress = Math.min(100, Math.max(0, Math.round(job.progress || 0)));
  byId("pipeline-progress").value = progress;
  byId("progress-number").textContent = progress + "%";
  byId("cancel-job").hidden = !activeJob(job);
  byId("retry-job").hidden = !stoppedStates.has(job.state);
  byId("cancel-job").disabled = jobMutation;
  byId("retry-job").disabled = jobMutation;
  const url = jobUrl(job.id);
  actionLink("job-log", stoppedStates.has(job.state) ? url + "/log" : null);
  const exportReady = clips.some((item) => item.status === "approved" && item.final_ready);
  actionLink("project-zip", exportReady ? url + "/zip" : null);
  actionLink("transcript-srt", job.transcript_available ? url + "/transcript.srt" : null);
  actionLink("transcript-json", job.transcript_available ? url + "/transcript.json" : null);
  byId("project-downloads").hidden = !exportReady && !job.transcript_available;
}

async function updateProjects(preferredId) {
  const jobs = await api("/api/jobs");
  const select = byId("job-select");
  const formatDate = new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeStyle: "short" });
  select.replaceChildren(...jobs.map((job) => {
    const date = new Date(job.created_at);
    const label = Number.isNaN(date.getTime()) ? job.filename : job.filename + " · " + formatDate.format(date);
    return new Option(label, job.id);
  }));
  byId("job-picker").hidden = !jobs.length;
  const id = jobs.find((job) => job.id === (preferredId || currentJobId))?.id || jobs[0]?.id;
  if (!id) return;
  select.value = id;
  if (id !== currentJobId) await selectProject(id);
}

async function selectProject(id) {
  currentJobId = id;
  currentJob = null;
  currentClipId = null;
  clipFormKey = "";
  const location = new URL(window.location.href);
  location.searchParams.set("job", id);
  window.history.replaceState(null, "", location);
  renderProject();
  await refreshProject();
}

async function refreshProject() {
  if (!currentJobId) return;
  clearTimeout(pollTimer);
  const sequence = ++projectRequest;
  try {
    const job = await api(jobUrl(currentJobId));
    if (sequence !== projectRequest) return;
    currentJob = job;
    notice("connection-error", "");
    renderProject();
    if (activeJob(job) || job.clips.some((clip) => clip.operation)) pollTimer = setTimeout(refreshProject, 1500);
  } catch (error) {
    if (sequence !== projectRequest) return;
    notice("connection-error", "Falha ao atualizar a gravação. Tentando novamente… " + error.message);
    pollTimer = setTimeout(refreshProject, 3500);
  }
}

byId("job-select").addEventListener("change", (event) => selectProject(event.target.value));

async function changeProject(action) {
  if (!currentJobId || jobMutation) return;
  jobMutation = true;
  renderProject();
  try {
    await api(jobUrl(currentJobId) + "/" + action, { method: "POST" });
    await refreshProject();
  } catch (error) {
    notice("connection-error", error.message);
  } finally {
    jobMutation = false;
    renderProject();
  }
}

byId("cancel-job").addEventListener("click", () => changeProject("cancel"));
byId("retry-job").addEventListener("click", () => changeProject("retry"));

async function changeClip(options, suffix = "") {
  if (!currentClipId || clipMutation) return false;
  const selection = clipUrl();
  clipMutation = true;
  renderProject();
  byId("clip-notice").className = "form-notice";
  notice("clip-notice", "Salvando…");
  try {
    await api(selection + suffix, options);
    if (clipUrl() === selection) {
      if (options.body && !JSON.parse(options.body).status) clipFormDirty = false;
      notice("clip-notice", suffix ? "Exportação enviada para a fila." : "Alteração salva.");
    }
    await refreshProject();
    return true;
  } catch (error) {
    if (clipUrl() === selection) {
      byId("clip-notice").className = "alert";
      notice("clip-notice", error.message);
    }
    return false;
  } finally {
    clipMutation = false;
    renderProject();
  }
}

function patchOptions(changes) {
  return { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(changes) };
}

clipForm.addEventListener("input", () => {
  const clip = selectedClip();
  if (!clip) return;
  clipFormDirty = true;
  updateClipActions(clip);
});
clipForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(clipForm));
  data.start = Number(data.start);
  data.end = Number(data.end);
  const clip = selectedClip();
  const keys = Object.keys(data).filter((field) => data[field] !== (clip[field] ?? ""));
  if (!keys.length) {
    clipFormDirty = false;
    updateClipActions(clip);
    return;
  }
  if (keys.includes("start") || keys.includes("end")) {
    const duration = data.end - data.start;
    const minimum = Number(currentJob.settings.minimum || 61);
    const maximum = Number(currentJob.settings.maximum || 180);
    if (duration < minimum - 0.00001 || duration > maximum + 0.00001 || data.end > currentJob.duration + 0.00001) {
      byId("clip-notice").className = "alert";
      notice("clip-notice", "Escolha um intervalo de " + minimum + " a " + maximum + " segundos dentro da gravação.");
      return;
    }
  }
  await changeClip(patchOptions(Object.fromEntries(keys.map((field) => [field, data[field]]))));
});

byId("approve-clip").addEventListener("click", () => {
  const clip = selectedClip();
  if (clip) changeClip(patchOptions({ status: clip.preview_ready ? "approved" : "pending" }));
});
byId("discard-clip").addEventListener("click", () => changeClip(patchOptions({ status: "discarded" })));
byId("retry-preview").addEventListener("click", () => changeClip(patchOptions({})));
byId("export-clip").addEventListener("click", () => changeClip({ method: "POST" }, "/export"));

async function initialize() {
  const requested = new URLSearchParams(window.location.search).get("job");
  const checks = await Promise.allSettled([
    api("/api/config").then((config) => {
      maxUploadBytes = config.max_upload_bytes;
      if (maxUploadBytes) notice("upload-limit", "· até " + formatBytes(maxUploadBytes));
      byId("ollama-option").hidden = !config.ollama_enabled;
    }),
    loadTracks(),
    updateProjects(requested),
  ]);
  if (checks.some(({ status }) => status === "rejected")) {
    notice("connection-error", "Não foi possível carregar todos os dados do estúdio. Atualize a página para tentar novamente.");
  }
}

initialize();
