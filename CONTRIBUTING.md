# 開発ガイド

シキめぐりの開発手順。プロダクトの説明は [README.md](README.md) を参照。

## 前提

**すべて Docker の中で動かす。** ホストに Python も仮想環境も要らない。
`docker compose` が使えれば足りる。

データは Firestore に置く。開発では compose に同梱した
**Firestore エミュレータ**が既定で立ち上がるので、GCP に繋がなくても動く。
`memory` ドライバはテスト専用で、プロセスが落ちると消える。

APIキーは不要。未設定のうちは外部サービスがすべて mock で動き、テストも一連の流れも通る。

## よく使うコマンド

```bash
# 開発サーバー（api + Firestore エミュレータ。編集すると自動リロード）
docker compose up api

# テスト
docker compose --profile test run --rm test

# アーキテクチャ図の再生成（docs/architecture.png）
docker compose --profile docs run --rm docs

# 紹介ページに載せる画面の写しを撮り直す（web/shots/*.png）
docker compose --profile shots up --build shots
```

ホストのポートが他プロジェクトと衝突するときは逃がす（コンテナ間の番号は変わらない）。

```bash
API_PORT=8090 FIRESTORE_PORT=8230 docker compose up api
```

エミュレータのデータを空にしたいときは、コンテナごと作り直す。

```bash
docker compose down && docker compose up api
```

## そのほかの構成

```bash
# api と agent を 2 サービスに分ける（本番と同じ構成）
# api は :8080、agent は :8091（コンテナ間は 8081）
AGENT_TRANSPORT=http docker compose --profile split up

# 保存先をプロセス内メモリにする（DB を立てずに動かしたいとき）
DB_DRIVER=memory docker compose up api

# フロントだけ単体で開く
docker compose --profile web up
```

## ディレクトリ構成

```
app/
├── domain/         # モデルと逆算タイムライン（外部 I/O なしの純粋ロジック）
├── adapters/       # 外部サービス。mock / live を同じインターフェースで差し替え
├── agents/         # Orchestrator と 4 つの子エージェント、会話の解釈（conversation.py）
├── infra/          # 永続化・監査ログ・時刻
├── api/            # API Gateway（Cloud Run: api）+ 画面の配信
└── agent/          # Orchestrator Agent（Cloud Run: agent）
services/           # api / agent / web / docs / shots の Dockerfile
web/                # 画面（PWA）。index.html=紹介ページ、console.html=アプリ本体
                    # アプリは 予定→衣装→確認→当日→返却 の一本道。1 画面に 1 段階だけ出し、
                    # 段階は state.event から autoStage() が決める（画面側に進行状態を持たない）
                    # 「AIに相談」は予定の登録前は出さず、以降はサイドバー（placeChat）
                    # 「予定」は登録前がフォーム、登録後は控えの表示に変わる
                    # 利用者 id はブラウザごと（localStorage）。認証が入るまでの代用
web/shots/          # 紹介ページに載せる画面の写し（docs/shots.js が撮る）
docs/               # アーキテクチャ図・画面の写しの生成スクリプト
tests/              # pytest
```

## 主なエンドポイント

api サービスでは `/api` 配下、agent サービスでは直下に生える。

| メソッド | パス | 用途 |
|---|---|---|
| POST | `/chat` | チャット 1 往復（会話の入口） |
| GET | `/chat/{uid}?after=` | 発言の取得。`after` で差分だけ拾う（画面のポーリング） |
| GET | `/stations` | 入力フォームの駅の選択肢 |
| POST | `/events/start` | 入力フォームからの受付。利用者登録〜衣装候補までを一度に進める |
| POST | `/events` | 式の登録（登録だけ。候補は出さない） |
| POST | `/events/{id}/outfits` | 衣装候補の提案 |
| GET | `/events/{id}/pickups` | 受取場所の比較 |
| POST | `/events/{id}/reservation` | 手配の**起案**（金銭は確定しない）。`pickup_id` で受取場所を指名、`replace` で確定済みの差し替え |
| POST | `/events/{id}/consents/{consent_id}` | 承認／却下。ここで初めて確定する |
| POST | `/events/{id}/return/check` | 返却期限との突合 |
| GET | `/audit?event_id=` | 監査ログ |
| POST | `/tasks/sweep` | 定期実行（Cloud Scheduler から叩く）。返却監視・TTL 削除 |
| POST | `/demo/seed` | 動作確認用（サンプルの式を投入する） |

動作確認用の操作は利用者の動線に混ぜない。画面から叩きたいときは
<http://localhost:8080/ui/console.html?dev=1> を開くと、最下部にだけ帯が出る。

## 実 API への切り替え

`.env.example` を `.env` にコピーし、キーを入れて該当モードを `live` にする。
分岐は `app/adapters/*` の `build_*_client()` に閉じているので、呼び出し側は変えない。

| 変数 | 対象 |
|---|---|
| `GEMINI_MODE` / `GEMINI_API_KEY` | 提案文の生成（慶弔マナー考慮） |
| `EKISPERT_MODE` / `EKISPERT_API_KEY` | 駅すぱあと API MCPサーバー（経路探索） |

`.env` はコミットしない。本番の秘密情報は Secret Manager で管理し、Cloud Run に
環境変数として注入する（イメージに焼き込まない）。

## 設計上の決まりごと

仕様の都合で、素直に書くと壊れる箇所がある。触るときは以下を守る。

1. **同意ゲートを迂回しない**
   金銭が動く操作は「起案（`propose`）」と「実行（`decide`）」を分ける。
   エージェントから予約 API を直接呼ぶコードを足さない。金額が小さくても例外にしない。

2. **慶弔の当事者情報を型に持たない**
   「誰の式か」「誰が亡くなったか」を保持するフィールドを追加しない。
   日時・会場・服装区分だけで全機能が成立することをテストで守っている。

3. **画像を受け取らない**
   顔写真・全身写真を扱う機能は持たない。保持するフィールドを型に足さない。

4. **判断したら監査ログを残す**
   エージェントが何かを決めたら `AuditTrail.record` に判断根拠（`basis`）を書く。
   自律実行か本人同意が要るかは `payload.autonomous_execution` で示す。

5. **保存は `Repository` 経由で行う**
   Firestore のクライアントを各所から直接触らない。`memory` と `firestore` の
   両実装が同じ振る舞いになるよう、追加した操作は両方に実装する。

6. **時刻は `Clock` 経由で取る**
   `datetime.now()` を直接呼ばない。テストは `FrozenClock` で当日朝を再現している。

7. **チャットと通知はアプリ内で完結させる**
   外部メッセージング基盤に依存しない（`app/adapters/chat.py`）。
   エージェント起点の通知は画面のポーリングで届く。端末へのプッシュは対象外。

8. **同じことを繰り返し通知しない**
   定期実行は何度も走る。同じ返却期限の注意を毎回送らないなど、冪等性に気を配る。

9. **確定済みの手配を黙って捨てない**
   予約が確定したあとに起案し直すときは、`replace=True` を要求し、
   **先に事業者側の予約を取り消してから**差し替える（`ArrangeAgent._release_current`）。
   上書きすると、事業者に予約が残ったままアプリ側だけ未予約になる。
   承認待ちの起案が残っている場合も `superseded` にして、承認札を二重に出さない。

10. **経路探索は同じ駅を叩かない**
    受取場所が会場の最寄りと同じことがある。駅すぱあとは同じ駅に経路を返さず、
    それでもアクセス数と使用料は加算される。`TransitClient.search` は mock も live も
    出発と目的地が同じなら探索せずに空の区間を返す。
    駅は名前ではなく**駅コード**で渡す（`EKISPERT_CODES`）。「大宮」「嵐山」のように同名の駅が
    あると名前では一つに決まらない。選べる駅を足したらコードも足す（足し忘れはテストで落ちる）。
    運賃は運賃と特急料金の合計で見る。運賃だけだと新幹線の経路が実際より安く出る。

11. **Gemini に金額や承認の文面を書かせない**
    利用者はその文を読んで承認する。金額・日時の書き違いや、入力に紛れた指示で
    文面が変わる余地を残さないため、承認を求める文はコードで組み立てる。
    Gemini に渡す材料は `facts_block()` を通し、出てきた文面は `reject_reason()` で検査する。
    材料の数値には単位を付けて渡す（数字だけだと「45分」を「45日」と書かれる）。

## テストの方針

- ドメインの純粋ロジック（`domain/timeline.py`）は入出力を直接検証する
- エージェントは `tests/conftest.py` の mock 一式と `FrozenClock` で動かす
- テストの保存先は `memory` ドライバ。エミュレータを立てずに速く回す
- ガバナンス（同意ゲート・データ最小化）は**振る舞いとしてテストで固定する**
- テスト名は日本語で、何を保証しているかがそのまま読めるようにする

## デプロイ

`main` への push で自動デプロイする GitHub Actions を用意してあるが、**いまはオフ**
（リポジトリ変数 `ENABLE_CD` が無いとジョブが skipped になる）。
有効化の手順は `.github/workflows/deploy.yml` の冒頭にある。

本番は Gemini と駅すぱあとを **live** で動かす。キーは Secret Manager の
`gemini-api-key` / `ekispert-api-key` から注入し、モデルはリポジトリ変数 `GEMINI_MODEL` で
変えられる（既定は `gemini-3.5-flash-lite`）。デプロイ後の疎通確認は `/health` の `modes` を見て、
どちらかが mock のままならジョブを失敗させる。

## 図と画面の写しを更新したとき

`docs/architecture.py` を編集したら図を作り直し、`docs/architecture.png` も一緒にコミットする。
README から参照しているため、画像がないと説明が欠ける。

画面を変えたら `web/shots/*.png` も撮り直してコミットする。紹介ページの「使い方」は
この写しで手順を見せているので、古いままだと説明と実物が食い違う。
