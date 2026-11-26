'use strict';

const $ = (id) => document.getElementById(id);
let key = '';
let cursor = '';
let audioURL = '';
let transcriptEditor = null;
let storageRetentionDays = 0;

function notice(message = '') {
  $('notice').textContent = message;
  $('notice').hidden = !message;
}

async function request(path, options = {}, raw = false) {
  const response = await fetch(path, {
    ...options,
    credentials: 'omit',
    headers: { Authorization: 'Bearer ' + key, 'Content-Type': 'application/json' }
  });
  if (!response.ok) {
    const failure = await response.json();
    throw new Error(failure.error);
  }
  return raw ? response.blob() : response.json();
}

function run(action) {
  return async (event) => {
    event?.preventDefault();
    notice();
    const button = event?.submitter || (event?.currentTarget?.tagName === 'BUTTON' ? event.currentTarget : null);
    if (button) button.disabled = true;
    try { await action(); }
    catch (error) { notice(error.message); }
    finally { if (button) button.disabled = false; }
  };
}

function paragraph(parent, text, tag = 'p') {
  const node = document.createElement(tag);
  node.textContent = text;
  parent.append(node);
  return node;
}

function renderCodeBlocks(node) {
  const text = node.textContent;
  if (!text.includes('```') && !text.includes('~~~')) return;
  const body = document.createDocumentFragment();
  let open = null;
  let lines = [];

  function finishBlock() {
    const value = lines.join('\n');
    if (open) {
      const pre = document.createElement('pre');
      paragraph(pre, value, 'code');
      body.append(pre);
    } else {
      const prose = value.replace(/^\n+|\n+$/g, '');
      if (prose) paragraph(body, prose);
    }
    lines = [];
  }

  for (const line of text.split(/\r\n|[\r\n]/)) {
    const marker = /^( {0,3})(`{3,}|~{3,})(.*)$/.exec(line);
    if (open) {
      if (marker && marker[2].startsWith(open.run) && /^[ \t]*$/.test(marker[3])) {
        finishBlock();
        open = null;
      } else {
        lines.push(line.replace(open.indent, ''));
      }
    } else if (marker && !(marker[2][0] === '`' && marker[3].includes('`'))) {
      finishBlock();
      open = { run: marker[2], indent: new RegExp('^ {0,' + marker[1].length + '}') };
    } else {
      lines.push(line);
    }
  }
  if (!open && !body.childNodes.length) return;
  finishBlock();
  node.replaceWith(body);
}

function button(parent, caption, action) {
  const node = paragraph(parent, caption, 'button');
  node.type = 'button';
  node.onclick = run(action);
}

function show(name) {
  for (const tab of document.querySelectorAll('button[data-view]')) {
    const active = tab.dataset.view === name;
    tab.setAttribute('aria-pressed', String(active));
    $(tab.dataset.view).hidden = !active;
  }
}

async function openSource(id) {
  show('journal');
  const item = await request('/v1/memory/' + id);
  $('detail').replaceChildren(entry(item));
  $('detail').hidden = false;
  button($('detail'), 'Close', () => { $('detail').hidden = true; });
  $('detail').scrollIntoView({ block: 'nearest' });
}

async function playClip(id, parent) {
  const blob = await request('/v1/recordings/' + id + '/audio', {}, true);
  document.querySelectorAll('audio').forEach((audio) => audio.remove());
  if (audioURL) URL.revokeObjectURL(audioURL);
  audioURL = URL.createObjectURL(blob);
  const audio = document.createElement('audio');
  audio.controls = true;
  audio.src = audioURL;
  parent.append(audio);
  await audio.play();
}

function playbackControl(parent, item) {
  if (item.audio_available === false) paragraph(parent, 'Audio expired.', 'small');
  else button(parent, 'Play recording', () => playClip(item.id, parent));
}

function entry(item) {
  const card = document.createElement('article');
  const meta = item.kind + ' · ' + new Date(item.started).toLocaleString() + ' · ' + item.device;
  paragraph(card, meta, 'small');
  const text = paragraph(card, item.text);
  if (item.kind === 'question' || item.kind === 'answer') renderCodeBlocks(text);
  for (const [index, id] of item.sources.entries()) {
    button(card, 'Open source ' + (index + 1), () => openSource(id));
  }
  if (item.kind === 'recording') {
    playbackControl(card, item);
    button(card, 'Edit transcript', () => openTranscript(item.id, (value) => { text.textContent = value; }));
  }
  return card;
}

async function search() {
  const params = new URLSearchParams({ q: $('query').value, day: $('day').value });
  const { items } = await request('/v1/memory?' + params);
  $('detail').hidden = true;
  $('results').replaceChildren(...items.map(entry));
  if (!items.length) paragraph($('results'), 'No archive entries match this search.');
}

async function status() {
  const data = await request('/v1/status');
  const remaining = (data.remaining_bytes / 2 ** 30).toFixed(1);
  const count = Object.values(data.recordings).reduce((sum, value) => sum + value, 0);
  $('status').textContent = count + ' recordings · ' + remaining + ' GiB of audio allowance remaining';
}

async function clips() {
  const { items } = await request('/v1/recordings?' + new URLSearchParams({ before: cursor }));
  for (const clip of items) {
    const card = document.createElement('article');
    paragraph(card, new Date(clip.started).toLocaleString() + ' · ' + clip.device +
      ' · ' + Math.round(clip.duration) + ' s · ' + clip.state.replaceAll('_', ' '), 'small');
    const text = paragraph(card, clip.transcript || '');
    if (clip.error) paragraph(card, clip.error);
    playbackControl(card, clip);
    button(card, 'Edit transcript', () => openTranscript(clip.id, (value) => { text.textContent = value; }));
    if (['failed', 'awaiting_transcription'].includes(clip.state)) {
      button(card, 'Retry transcription', async () => {
        await request('/v1/recordings/' + clip.id + '/retry', { method: 'POST' });
        notice('Recording queued.');
      });
    }
    $('clips').append(card);
  }
  const last = items.at(-1);
  cursor = last ? last.started + ',' + last.id : '';
  $('older').hidden = items.length < 35;
  if (!$('clips').childElementCount) paragraph($('clips'), 'No recordings have been uploaded.');
}

function displayTranscript(snapshot) {
  transcriptEditor.snapshot = snapshot;
  transcriptEditor.pending = null;
  const draft = $('transcript-draft');
  draft.value = snapshot.text || '';
  draft.disabled = ['queued', 'processing'].includes(snapshot.state);
  $('save-transcript').disabled = true;
  $('transcript-status').textContent = draft.disabled ? 'The clip is still processing. Reload it before editing.' : '';
  const history = $('transcript-history');
  history.replaceChildren();
  if (snapshot.history.length) paragraph(history, 'Earlier transcript versions', 'h3');
  for (const revision of snapshot.history) {
    const row = document.createElement('details');
    paragraph(row, new Date(revision.changed).toLocaleString() + ' · ' + revision.editor, 'summary');
    paragraph(row, revision.previous_text ?? 'No previous transcript');
    history.append(row);
  }
}

async function openTranscript(id, onSave) {
  if ($('transcript-dialog').open) return;
  const path = '/v1/recordings/' + id + '/correction';
  const snapshot = await request(path);
  if ($('transcript-dialog').open) return;
  transcriptEditor = { path, onSave };
  displayTranscript(snapshot);
  $('transcript-dialog').showModal();
}

function editTranscript(action) {
  return async (event) => {
    event.preventDefault();
    if (!transcriptEditor || transcriptEditor.busy) return;
    const active = transcriptEditor;
    active.busy = true;
    $('save-transcript').disabled = true;
    $('reload-transcript').disabled = true;
    $('close-transcript').disabled = true;
    try { await action(active); }
    catch (error) { $('transcript-status').textContent = error.message; }
    finally {
      active.busy = false;
      $('reload-transcript').disabled = false;
      $('close-transcript').disabled = false;
      updateSaveButton();
    }
  };
}

function updateSaveButton() {
  const draft = $('transcript-draft');
  const value = draft.value.trim();
  $('save-transcript').disabled = !transcriptEditor || transcriptEditor.busy || draft.disabled ||
    !value || value === transcriptEditor.snapshot.text || [...value].length > 12000;
}

$('transcript-draft').oninput = updateSaveButton;
$('close-transcript').onclick = () => $('transcript-dialog').close();
$('transcript-dialog').addEventListener('cancel', (event) => {
  if (transcriptEditor?.busy) event.preventDefault();
});
$('reload-transcript').onclick = editTranscript(async ({ path }) => displayTranscript(await request(path)));
$('transcript-form').onsubmit = editTranscript(async (editor) => {
  const text = $('transcript-draft').value.trim();
  if (!editor.pending || editor.pending.text !== text) {
    editor.pending = { text, id: crypto.randomUUID(), revision: editor.snapshot.revision };
  }
  displayTranscript(await request(editor.path, { method: 'PUT', body: JSON.stringify(editor.pending) }));
  editor.onSave(editor.snapshot.text);
  $('transcript-status').textContent = 'Correction saved.';
});

async function loadModels(changes) {
  const data = await request('/v1/admin/models', changes ? { method: 'PUT', body: JSON.stringify(changes) } : {});
  const select = $('assets');
  select.replaceChildren(...data.files.map((value) => {
    const option = document.createElement('option');
    option.value = value;
    return option;
  }));
  for (const [name, value] of Object.entries(data.settings)) {
    const field = $('configuration').elements[name];
    if (field.type === 'checkbox') field.checked = value;
    else field.value = value;
  }
  const speech = data.models.speech.join(', ') || 'device voices';
  const mode = { mac_connected: 'Mac model connected', mac_waiting: 'Waiting for Mac relay' };
  $('model-status').textContent = 'Chat: ' + (mode[data.models.chat] || data.models.chat.replaceAll('_', ' ')) +
    ' · AAC decoder: ' + (data.models.decoder === 'configured' ? 'configured' : 'awaiting decoder') +
    ' · Transcription: ' + data.models.transcription.replaceAll('_', ' ') + ' · Speech: ' + speech;
  if (data.chat_error) notice(data.chat_error);
}

function showStorage(data) {
  const gib = 2 ** 30;
  const fields = $('storage-settings').elements;
  fields.audio_limit_gib.value = data.audio_limit_bytes === null ? '' : data.audio_limit_bytes / gib;
  fields.retention_days.value = data.retention_days;
  storageRetentionDays = data.retention_days;
  $('storage-status').textContent = (data.audio_bytes / gib).toFixed(1) + ' GiB archived · ' +
    (data.remaining_bytes / gib).toFixed(1) + ' GiB available for Nex · ' +
    (data.disk_reserve_bytes / gib).toFixed(1) + ' GiB disk reserve';
  if (data.ssd_limit_bytes) {
    const gb = 10 ** 9;
    $('storage-status').textContent += ' · ' + (data.ssd_audio_bytes / gb).toFixed(2) + ' GB on SSD · ' +
      (data.data_audio_bytes / gb).toFixed(2) + ' GB on data drive';
    if (data.ssd_offloading) $('storage-status').textContent += ' · Moving older recordings';
  }
  $('storage-settings').hidden = false;
}

$('access').onsubmit = run(async () => {
  key = $('key').value.trim();
  try { await status(); } catch (error) { key = ''; throw error; }
  $('key').value = '';
  $('login').hidden = true;
  $('workspace').hidden = false;
  await search();
});
$('lock').onclick = () => location.reload();
$('search').onsubmit = run(search);
$('recordings').onclick = run(async () => {
  cursor = '';
  $('clips').replaceChildren();
  await clips();
});
$('older').onclick = run(clips);
$('question').onsubmit = run(async () => {
  const area = $('answer');
  area.replaceChildren();
  const text = $('message').value.trim();
  const answer = await request('/v1/chat', {
    method: 'POST',
    body: JSON.stringify({ id: crypto.randomUUID(), text, time_zone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC' })
  });
  area.append(entry({ ...answer, kind: 'answer', device: answer.engine.replaceAll('_', ' '),
    started: new Date().toISOString() }));
  $('message').value = '';
});
$('reload').onclick = run(() => loadModels());
$('configuration').onsubmit = run(async () => {
  const settings = Object.fromEntries(new FormData($('configuration')));
  for (const name of ['context_size', 'threads', 'gpu_layers']) settings[name] = Number(settings[name]);
  settings.use_mac_model = $('configuration').elements.use_mac_model.checked;
  await loadModels(settings);
  notice('Model settings saved.');
});
$('storage-settings').onsubmit = run(async () => {
  const raw = Object.fromEntries(new FormData($('storage-settings')));
  const policy = {
    audio_limit_gib: raw.audio_limit_gib === '' ? null : Number(raw.audio_limit_gib),
    retention_days: Number(raw.retention_days)
  };
  if (policy.retention_days > 0 && policy.retention_days !== storageRetentionDays &&
      !window.confirm('Expire processed audio older than ' + policy.retention_days +
                      ' days? Nex keeps the searchable archive entries.')) return;
  showStorage(await request('/v1/admin/storage', { method: 'PUT', body: JSON.stringify(policy) }));
  notice('Storage settings saved.');
});
for (const tab of document.querySelectorAll('button[data-view]')) {
  tab.onclick = run(async () => {
    show(tab.dataset.view);
    if (tab.dataset.view === 'models') await loadModels();
    if (tab.dataset.view === 'journal') await status();
    if (tab.dataset.view === 'storage') showStorage(await request('/v1/admin/storage'));
  });
}
