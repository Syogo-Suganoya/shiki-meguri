# デプロイ手順

シキめぐりを Google Cloud（Cloud Run）へ載せる手順。
**CLI（gcloud）**・**画面（Cloud Console）**・**GitHub Actions（CD）** の 3 通りを書く。
CD は現在オフにしてある（パターン C 参照）。

開発手順は [CONTRIBUTING.md](CONTRIBUTING.md)、プロダクトの説明は [README.md](README.md)。

## 構成

MVP は **1 サービス構成**でデプロイする。`api` コンテナが `AGENT_TRANSPORT=inproc` で
エージェントを同一プロセスに載せるため、Cloud Run サービスは 1 つで足りる。

```
Cloud Scheduler ──POST /api/tasks/sweep──▶ Cloud Run（api）──▶ Firestore
                                                    │           Cloud Logging
                                                    └──▶ Gemini / 駅すぱあと
                                              Secret Manager（APIキー）
```

> **2 サービス構成（api + agent）について**
> `AGENT_TRANSPORT=http` は compose 上では動くが、Cloud Run で agent を非公開にすると
> api からの呼び出しに ID トークンが要る。その付与は未実装なので、
> 現状のコードで分割してデプロイするなら agent も公開する必要がある。
> MVP では 1 サービス構成を使う。

## 事前準備（CLI・画面 共通）

必要なもの: 課金が有効な GCP プロジェクト、`gcloud`（CLI の場合）。

有効化する API:
`run.googleapis.com` / `cloudbuild.googleapis.com` / `artifactregistry.googleapis.com` /
`firestore.googleapis.com` / `secretmanager.googleapis.com` / `cloudscheduler.googleapis.com`

以降はこの値で書く。別のプロジェクトに載せるなら読み替える。

| 項目 | 値 |
|---|---|
| プロジェクトID | `shiki-meguri` |
| リージョン | `asia-northeast1`（東京） |
| Artifact Registry リポジトリ | `shiki-meguri` |
| Cloud Run サービス名 | `shiki-api` |

---

# パターン A: CLI（gcloud）

## 1. プロジェクトと API

```bash
export PROJECT_ID=shiki-meguri
export REGION=asia-northeast1
export REPO=shiki-meguri
export SERVICE=shiki-api

gcloud config set project "$PROJECT_ID"
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  firestore.googleapis.com secretmanager.googleapis.com cloudscheduler.googleapis.com
```

## 2. Firestore（Native モード）

アプリの保存先。開発でエミュレータに入っていたデータが、そのまま本物に載る。

```bash
gcloud firestore databases create --location="$REGION"
```

## 3. Artifact Registry

```bash
gcloud artifacts repositories create "$REPO" \
  --repository-format=docker \
  --location="$REGION" \
  --description="シキめぐりのコンテナイメージ"
```

## 4. シークレット（実APIを使う場合のみ）

キーを入れない間は全て mock で動くので、この節は飛ばしてよい。

```bash
printf '%s' "$GEMINI_API_KEY"  | gcloud secrets create gemini-api-key  --data-file=-
printf '%s' "$EKISPERT_KEY"    | gcloud secrets create ekispert-api-key --data-file=-
```

Cloud Run のサービスアカウントに読み取り権限を与える。

```bash
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

for s in gemini-api-key ekispert-api-key; do
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role=roles/secretmanager.secretAccessor
done
```

## 5. イメージのビルド

ローカルの compose と同じ Dockerfile を使う。リポジトリ直下に Dockerfile が無いため、
`--source` ではなく `cloudbuild.yaml` を指定する。

```bash
gcloud builds submit --config cloudbuild.yaml \
  --substitutions="_REGION=${REGION},_REPO=${REPO}"
```

## 6. デプロイ

```bash
gcloud run deploy "$SERVICE" \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/api:latest" \
  --region="$REGION" \
  --allow-unauthenticated \
  --set-env-vars="APP_ENV=prod,AGENT_TRANSPORT=inproc,DB_DRIVER=firestore,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},AGENT_SPEND_LIMIT_YEN=0"
```

実APIに切り替えるときは、上のコマンドに以下を足す。

```bash
  --set-env-vars="GEMINI_MODE=live,EKISPERT_MODE=live" \
  --set-secrets="GEMINI_API_KEY=gemini-api-key:latest,EKISPERT_API_KEY=ekispert-api-key:latest"
```

デプロイ後の URL を控える。

```bash
SERVICE_URL=$(gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')
curl -s "$SERVICE_URL/health"
```

## 7. Firestore の TTL ポリシー

式の終了 + 7 日で `events` を自動削除する（設計書 §7）。
**`ttl_at` は Timestamp 型で保存している**ため、そのままポリシーを張れる。

```bash
gcloud firestore fields ttls update ttl_at \
  --collection-group=events \
  --enable-ttl
```

反映まで 10 分ほどかかる。`audit` は追記専用なので TTL を張らない。

## 8. Cloud Scheduler（定期実行）

返却期限の監視と TTL 超過の検知をまとめて回す。

```bash
gcloud scheduler jobs create http shiki-sweep \
  --location="$REGION" \
  --schedule="*/10 * * * *" \
  --time-zone="Asia/Tokyo" \
  --uri="${SERVICE_URL}/api/tasks/sweep" \
  --http-method=POST
```

式当日の動線監視を細かくしたいなら `*/5 * * * *` まで縮める。

## 9. 動作確認

```bash
curl -s "$SERVICE_URL/health"
curl -s -X POST "$SERVICE_URL/api/chat" \
  -H 'content-type: application/json' \
  -d '{"uid":"smoke","text":"明日16時、品川の結婚式にお呼ばれ"}'
```

ブラウザで `"$SERVICE_URL"/ui/` を開き、チャットから承認まで通ることを見る。

---

# パターン B: 画面（Cloud Console）

画面だけで完結させる場合、**イメージのビルドだけは手を動かす場所が要る**。
GitHub と繋ぐか、Cloud Shell を使う。以下は Cloud Shell を使う前提で書く
（画面右上の `>_` アイコンで開く。ブラウザ内のターミナルなので、ローカル環境は汚れない）。

## 1. プロジェクトと API

1. 画面上部のプロジェクト選択から `shiki-meguri` を選ぶ
2. ナビゲーションメニュー → **API とサービス** → **ライブラリ**
3. 次を検索して、それぞれ「有効にする」
   - Cloud Run Admin API / Cloud Build API / Artifact Registry API
   - Firestore API / Secret Manager API / Cloud Scheduler API

## 2. Firestore

1. ナビゲーションメニュー → **Firestore**
2. **データベースを作成**
3. **Native モード**を選ぶ
4. ロケーションに `asia-northeast1` を選び、**データベースを作成**

## 3. Artifact Registry

1. ナビゲーションメニュー → **Artifact Registry** → **リポジトリ**
2. **リポジトリを作成**
3. 名前 `shiki-meguri` / 形式 **Docker** / ロケーション `asia-northeast1`
4. **作成**

## 4. シークレット（実APIを使う場合のみ）

1. ナビゲーションメニュー → **Secret Manager** → **シークレットを作成**
2. 名前 `gemini-api-key`、シークレットの値にキーを貼って **シークレットを作成**
3. 駅すぱあと分も同様に作る

権限は、後の Cloud Run のデプロイ画面でシークレットを参照したときに
「権限を付与しますか」と聞かれるので、その場で許可すれば足りる。

## 5. イメージのビルド（Cloud Shell）

1. 画面右上の **Cloud Shell をアクティブにする**（`>_`）
2. 開いたターミナルで、リポジトリを取得してビルドする

```bash
gcloud config set project shiki-meguri
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=_REGION=asia-northeast1,_REPO=shiki-meguri
```

Artifact Registry の画面に `api` イメージが増えていれば成功。

## 6. Cloud Run へデプロイ

1. ナビゲーションメニュー → **Cloud Run** → **コンテナをデプロイ** → **サービス**
2. **既存のコンテナ イメージから 1 つのリビジョンをデプロイする**を選び、
   **選択** から Artifact Registry の `shiki-meguri/api:latest` を選ぶ
3. サービス名 `shiki-api`、リージョン `asia-northeast1`
4. 認証: **未認証の呼び出しを許可**（デモ用に公開する場合）
5. **コンテナ、ボリューム、ネットワーキング、セキュリティ** を開く
6. **変数とシークレット** タブで環境変数を追加

   | 名前 | 値 |
   |---|---|
   | `APP_ENV` | `prod` |
   | `AGENT_TRANSPORT` | `inproc` |
   | `DB_DRIVER` | `firestore` |
   | `GOOGLE_CLOUD_PROJECT` | `shiki-meguri` |
   | `AGENT_SPEND_LIMIT_YEN` | `0` |

> `FIRESTORE_EMULATOR_HOST` は**本番では設定しない**。設定されているとクライアントが
> エミュレータを探しに行き、本物の Firestore に繋がらない。

7. 実APIを使うなら、同じ画面で `GEMINI_MODE=live` などを追加し、
   **シークレットを参照** から `GEMINI_API_KEY` ← `gemini-api-key:latest` のように紐づける
8. **作成**

完了すると画面上部に `https://shiki-api-….run.app` が出る。控えておく。

## 7. Firestore の TTL ポリシー

1. ナビゲーションメニュー → **Firestore** → 対象のデータベースを選ぶ
2. 左メニューの **有効期間（TTL）** → **ポリシーを作成**
3. コレクション グループ `events`、タイムスタンプ フィールド `ttl_at`
4. **作成**（反映まで 10 分ほどかかる）

## 8. Cloud Scheduler

1. ナビゲーションメニュー → **Cloud Scheduler** → **ジョブを作成**
2. 名前 `shiki-sweep` / リージョン `asia-northeast1`
3. 頻度 `*/10 * * * *` / タイムゾーン `日本標準時（JST）`
4. **続行** → ターゲットのタイプ **HTTP**
5. URL に `https://shiki-api-….run.app/api/tasks/sweep`、HTTP メソッド **POST**
6. **作成**
7. 一覧の該当ジョブで **強制実行** を押し、結果が成功になることを確認する

## 9. 動作確認

Cloud Run の画面に出ている URL の末尾に `/ui/` を付けて開く。
チャットに「明日16時、品川の結婚式にお呼ばれ」と送り、候補提示 → 承認 → タイムラインまで
進めば成功。エージェントの判断は Cloud Run の **ログ** タブにも構造化ログとして出る。

---

# パターン C: GitHub Actions（CD）

`main` への push で自動デプロイする。ワークフローは
[.github/workflows/deploy.yml](.github/workflows/deploy.yml)。

> **いまはオフにしてある。**
> ジョブに `if: vars.ENABLE_CD == 'true'` を掛けてあり、リポジトリ変数 `ENABLE_CD` が
> 無いうちは push しても手動実行してもジョブは skipped になる。GCP には一切触れない。

やることは 3 つ。1〜2 は有効化するときに一度だけ行う。

## 1. デプロイ用サービスアカウント

```bash
export PROJECT_ID=shiki-meguri
export REPO_SLUG=Syogo-Suganoya/shiki-meguri

gcloud iam service-accounts create github-deployer \
  --display-name="GitHub Actions からのデプロイ用" --project="$PROJECT_ID"

DEPLOY_SA="github-deployer@${PROJECT_ID}.iam.gserviceaccount.com"

for role in roles/run.admin roles/cloudbuild.builds.editor \
            roles/artifactregistry.writer roles/iam.serviceAccountUser \
            roles/storage.admin; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${DEPLOY_SA}" --role="$role"
done
```

`iam.serviceAccountUser` は Cloud Run のランタイム SA を「使う」ために要る。
`storage.admin` は Cloud Build がソースを置くバケット用。

## 2. Workload Identity 連携（鍵を置かない）

サービスアカウントの JSON 鍵を GitHub に置くのは避け、OIDC で短命の資格情報を得る。

```bash
gcloud iam workload-identity-pools create github \
  --location=global --project="$PROJECT_ID"

gcloud iam workload-identity-pools providers create-oidc github \
  --location=global --workload-identity-pool=github \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='${REPO_SLUG}'" \
  --project="$PROJECT_ID"
```

`attribute-condition` を必ず入れる。これが無いと**他人のリポジトリからも**この
サービスアカウントを借りられてしまう。

このリポジトリからの借用だけを許可する。

```bash
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
POOL="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github"

gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/${POOL}/attribute.repository/${REPO_SLUG}" \
  --project="$PROJECT_ID"

echo "GCP_WIF_PROVIDER=${POOL}/providers/github"
echo "GCP_DEPLOY_SA=${DEPLOY_SA}"
```

## 3. GitHub 側の設定

Settings → Secrets and variables → **Actions** → **Variables** に登録する
（いずれも秘密情報ではないので Secrets ではなく Variables でよい）。

| 変数 | 値 | 必須 |
|---|---|---|
| `ENABLE_CD` | `true`（**これを入れるまで CD は動かない**） | ○ |
| `GCP_WIF_PROVIDER` | 上の `echo` が出した provider のパス | ○ |
| `GCP_DEPLOY_SA` | `github-deployer@shiki-meguri.iam.gserviceaccount.com` | ○ |
| `GCP_PROJECT_ID` | `shiki-meguri`（既定値と同じなら省略可） | — |
| `GCP_REGION` | `asia-northeast1`（同上） | — |
| `GCP_ARTIFACT_REPO` | `shiki-meguri`（同上） | — |
| `GCP_RUN_SERVICE` | `shiki-api`（同上） | — |

## 動き

1. `main` に push（`app/` `web/` `services/` `tests/` などが変わったとき）
2. ローカルと同じコンテナでテストを実行。落ちたらここで止まる
3. Cloud Build でイメージをビルドし、コミットSHAをタグにして push
4. Cloud Run へデプロイ
5. `/health` を叩いて疎通確認

初回だけは**パターン A か B で一度デプロイしておく**とよい。Firestore・
Artifact Registry・Secret Manager・TTL ポリシー・Cloud Scheduler の作成は
CD に含めていない（作り直しの事故を避けるため、インフラは手で作る）。

## 止めたいとき

- `ENABLE_CD` を `false` にするか、変数ごと削除する
- または Actions タブ → 該当ワークフロー → 右上 "..." → **Disable workflow**

---

# 運用上の注意

- **`/api/tasks/sweep` が公開される**
  未認証呼び出しを許可すると、この定期実行エンドポイントも外から叩ける。
  実行内容は返却期限の監視と通知が中心だが、TTL 超過イベントの検知も含む。
  本番では Cloud Run を非公開にして Cloud Scheduler に OIDC トークンを付ける
  （`--oidc-service-account-email`）か、このパスだけ別サービスに分ける。

- **`FIRESTORE_EMULATOR_HOST` を本番に持ち込まない**
  開発では compose がこの変数でエミュレータを指している。本番で設定されていると
  クライアントがエミュレータを探しに行き、本物の Firestore に繋がらない。

- **画像の保存先は用意しない**
  Cloud Storage のバケットは要らない。顔写真・全身写真を受け取らない設計なので、
  永続化先を作ると設計書 §7-1 に反する。

- **秘密情報をイメージに焼かない**
  `.env` はコミットもデプロイもしない。キーは必ず Secret Manager 経由で注入する。

- **Cloud Run のサービスアカウントに Firestore の権限が要る**
  既定のランタイム SA には通常付いているが、専用 SA を使うなら
  `roles/datastore.user` を付ける。

- **リージョンを揃える**
  Cloud Run・Firestore・Artifact Registry・Cloud Scheduler は
  すべて `asia-northeast1` に置く。Firestore のロケーションは後から変更できない。
