'use strict';

const $ = (id) => document.getElementById(id);

function notice(message = '') {
  $('notice').textContent = message;
  $('notice').hidden = !message;
}

function showLink(link, created = false) {
  $('welcome').hidden = true;
  $('create').hidden = true;
  $('owner').reset();
  $('link').value = link;
  $('created').hidden = !created;
  $('connect').hidden = false;
  $('connect-heading').focus();
}

async function requestSetup(options) {
  const response = await fetch('/setup', { credentials: 'omit', ...options });
  const result = await response.json();
  return { response, result };
}

async function loadSetup() {
  $('retry').hidden = true;
  notice('Checking the server…');
  try {
    const { response, result } = await requestSetup();
    if (!response.ok) throw new Error(result.error);
    if (result.owner_exists) showLink(result.server_link);
    else $('welcome').hidden = false;
    notice();
  } catch (error) {
    notice(error.message);
    $('retry').hidden = false;
  }
}

$('retry').addEventListener('click', loadSetup);
$('start').addEventListener('click', () => {
  $('welcome').hidden = true;
  $('create').hidden = false;
  $('username').focus();
});

$('owner').addEventListener('submit', async (event) => {
  event.preventDefault();
  const password = $('password').value;
  if (password !== $('confirmation').value) {
    notice('Passwords do not match.');
    $('confirmation').focus();
    return;
  }
  const button = $('owner').querySelector('button');
  button.disabled = true;
  notice('Creating your account…');
  try {
    const { response, result } = await requestSetup({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: $('username').value, password }),
    });
    if (response.status === 409) return await loadSetup();
    if (!response.ok) throw new Error(result.error);
    showLink(result.server_link, true);
    notice();
  } catch (error) {
    notice(error.message);
  } finally {
    button.disabled = false;
  }
});

$('copy').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText($('link').value);
    notice('Server link copied.');
  } catch {
    $('link').select();
    notice('Copy the selected server link.');
  }
});

loadSetup();
