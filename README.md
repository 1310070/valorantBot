# ValorantBot2 ドキュメント

## 概要
ValorantBot2 は Discord サーバー向けの支援ボットです。Discord のスラッシュコマンドと組み合わせて、VALORANT の募集 DM の送信、tracker.gg プロフィール URL の生成、ストア情報の取得を自動化します。ボットとは別スレッドで FastAPI サーバーが起動し、Chrome 拡張から Riot 認証 Cookie を安全に受け取って PostgreSQL に暗号化保存します。

## 主な機能
- `/call` — 選択したメンバーに募集 DM を送信し、返信を UI から収集します。
- `/profile` — 入力した Riot ID から tracker.gg のプロフィール URL を作成します。
- `/store` — 登録済みの Riot 認証 Cookie を使って VALORANT のストアを取得し、スキン情報を Embed で表示します。失敗した場合は診断ボタンが表示され、Cloudflare ブロックや再ログイン要求などを調査できます。
- FastAPI エンドポイント `/nonce` と `/riot-cookies` — Chrome 拡張から送られる Cookie を受け取り、Discord ユーザー ID ごとに暗号化して保存します。
- `scripts/diag_reauth.py` — CLI から再認証の成否を総当たりで確認する診断ツールです。

## ディレクトリ構成
```
valorantBot/
├── Dockerfile
├── README.md
├── requirements.txt
├── riot-cookie-extension/      # Cookie 収集用 Chrome 拡張
└── valorantBot2/
    ├── bot.py                  # Discord ボットのエントリーポイント
    ├── rec.py                  # FastAPI アプリケーション
    ├── cogs/                   # Discord UI (スラッシュコマンド定義)
    ├── services/               # ストア取得・DB・診断などのサービス層
    ├── scripts/                # 診断用スクリプト
    └── views/                  # Discord UI コンポーネント(View, Modal, Button)
```

## 動作要件
- Python 3.11 以上（Dockerfile は `python:3.11-slim` を使用）
- PostgreSQL 13 以降（ユーザー Cookie 保存用）
- Discord Bot トークン
- Riot 認証 Cookie（Chrome 拡張で収集）

## 環境変数
| 変数名 | 必須 | 説明 |
| --- | --- | --- |
| `DISCORD_TOKEN` | 必須 | Discord ボットのトークン。 |
| `DATABASE_URL` または `DB_DSN` | 必須 | PostgreSQL への接続文字列。`postgresql://` 形式を推奨します。 |
| `COOKIE_ENC_KEY` | 推奨 | Cookie 暗号化に利用する Base64 文字列。未設定の場合は起動のたびにランダム生成されるため、永続保存が必要なら固定値を設定してください。`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` などで生成できます。 |
| `STARTUP_CHANNEL_ID` | 任意 | 起動時の案内メッセージを送るチャンネル ID。未設定の場合はシステムチャンネルや送信可能なテキストチャンネルを自動探索します。 |
| `PORT` | 任意 | FastAPI サーバーの公開ポート。既定は `8190` です。 |
| `VALORANT_COOKIES_DIR` | 任意 | ファイルベースの Cookie 参照先を追加で指定したい場合に利用します。 |

## セットアップ手順
1. リポジトリをクローンし、Python 仮想環境を作成します。
   ```bash
   git clone <this-repo>
   cd valorantBot
   python -m venv .venv
   source .venv/bin/activate
   ```
2. 依存ライブラリをインストールします。
   ```bash
   pip install -r requirements.txt
   ```
3. `.env` などに前述の環境変数を設定します。PostgreSQL のテーブルはボット起動時または FastAPI 起動時に自動作成されます。

## ボットの起動
ローカルで起動する場合:
```bash
python -m valorantBot2.bot
```
ボット起動時に FastAPI サーバーもバックグラウンドスレッドで起動します。

Docker を利用する場合:
```bash
docker build -t valorant-bot .
docker run --rm \
  -e DISCORD_TOKEN=... \
  -e DATABASE_URL=... \
  -e COOKIE_ENC_KEY=... \
  -p 8190:8190 \
  valorant-bot
```

## FastAPI エンドポイント
- `GET /nonce` — ワンタイムノンスを発行します。180 秒で失効します。
- `POST /riot-cookies` — Chrome 拡張から送られる JSON を受信し、Discord ユーザー ID と Cookie を紐づけて暗号化保存します。`nonce`, `user_id`, `cookies.auth`, `cookies.puuid` を含む必要があります。
- `GET /` — ヘルスチェック用の軽量レスポンスを返します。

## Chrome 拡張による Cookie 登録
`riot-cookie-extension/` フォルダを Chrome の「デベロッパーモード」から読み込み、有効化してください。Discord のユーザー ID を入力してボタンを押すと、以下が自動実行されます。
1. `https://auth.riotgames.com` の Cookie（`ssid`, `sub`, `clid`, `tdid`, `csid`）を取得。
2. `/nonce` から取得したノンスと合わせて `/riot-cookies` に送信。
3. 成功時は拡張上に `ok: true` が表示されます。ボット側で `/store` コマンドが利用可能になります。

## スラッシュコマンド詳細
- **/call**: ゲーム・人数・対象（オンライン/オフライン）を指定し、参加可否ボタン付きの DM を対象メンバーに送信します。返信内容は募集主に DM されます。
- **/profile**: Riot ID（名前 + タグ）を受け取り、tracker.gg のプロフィール URL を生成してボタン付きで返します。
- **/store**: 保存済み Cookie で Riot 認証を行い、武器スキンの価格とアイコンを Embed で表示します。Cookie が未登録の場合は登録手順の Embed を案内します。取得に失敗した場合は「診断を実行」ボタンが表示され、再認証の詳細ログ（マスク済みテキストまたは添付ファイル）を受け取れます。

## 診断ツールとトラブルシューティング
- `/store` の診断ボタンは `services/reauth_diag.collect_reauth_diag` を呼び出し、Cloudflare ブロックや `login_required` などの原因をレポートします。
- CLI で詳細なログを確認したい場合は `python -m valorantBot2.scripts.diag_reauth <discord_user_id>` を実行してください。DB またはファイルの Cookie を総当たりで再認証し、HTTP ステータスと成否を出力します。
- ストア取得 API は Riot 側の 403 応答時にプロキシや出口 IP の変更を促すメッセージを返します。必要に応じて `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` などを環境変数で設定してください。

## ライセンス
本リポジトリのライセンスが未記載の場合は、プロジェクトオーナーに確認してください。
