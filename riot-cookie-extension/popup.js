async function getCookies(url) {
  return new Promise((resolve, reject) => {
    chrome.cookies.getAll({ url }, (cookies) => {
      if (chrome.runtime.lastError) {
        reject(chrome.runtime.lastError);
      } else {
        resolve(cookies);
      }
    });
  });
}

async function collectAndSend() {
  const statusEl = document.getElementById('status');
  const outEl = document.getElementById('out');
  const userId = document.getElementById('userId').value.trim();
  statusEl.textContent = '';
  outEl.textContent = '';

  if (!/^\d+$/.test(userId)) {
    statusEl.textContent = 'ユーザーIDが正しくありません';
    statusEl.className = 'err';
    return;
  }

  try {
    const authCookies = await getCookies('https://auth.riotgames.com');

    const auth = {};
    ['ssid', 'sub', 'clid', 'tdid', 'csid'].forEach((name) => {
      const c = authCookies.find((v) => v.name === name);
      if (c) auth[name] = c.value;
    });

    const puuidRes = await fetch('https://auth.riotgames.com/userinfo', {
      credentials: 'include',
    });
    const puuidData = await puuidRes.json();
    const puuid = puuidData && puuidData.sub;

    const baseUrl = 'https://pure-cherrita-inosuke-6597cf0f.koyeb.app:8190';

    const nonceRes = await fetch(`${baseUrl}/nonce`);
    const { nonce } = await nonceRes.json();

    const body = {
      nonce,
      user_id: userId,
      cookies: { auth, puuid },
    };

    const res = await fetch(`${baseUrl}/riot-cookies`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();

    if (data.ok) {
      statusEl.textContent = '送信しました';
      statusEl.className = 'ok';
      outEl.textContent = JSON.stringify(data, null, 2);
    } else {
      statusEl.textContent = data.error || '送信に失敗しました';
      statusEl.className = 'err';
      outEl.textContent = JSON.stringify(data, null, 2);
    }
  } catch (e) {
    statusEl.textContent = 'エラー: ' + e;
    statusEl.className = 'err';
  }
}

document.getElementById('btn').addEventListener('click', collectAndSend);
