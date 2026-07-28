# Tracewright

[English](README.md) | [繁體中文](README.zh-TW.md)

**一套會安全回答，並且把判斷過程攤開給你看的客服 AI。**

Tracewright 把一則客戶訊息交給一連串小型 AI 步驟處理（理解問題 → 檢查風險 →
查找事實 → 產生回覆 → 驗證回覆），管理者可以在 Web Console **即時看著每一步
發生**。模型沒把握、或訊息看起來有風險時，它會轉交人工，而不是猜。

專案名字裡的重點就是 **trace**：每則訊息都會留下一份可點開的完整紀錄，讓你
知道 AI 為什麼這樣回答——那通常是 AI 產品裡最難交代的一段。

> ⚠️ **這是 Demo，不是正式系統。** 使用小型開源模型與兩組靜態 Demo Token。
> 適合展示、學習與後續開發，不適合無人監督地面對真實客戶。見
> [安全邊界與限制](#安全邊界與限制)。

**接著看哪裡：** 想調整行為（不寫 Python）看 [TUNING.md](TUNING.md)；
要接手程式碼看 [DEVELOPING.md](DEVELOPING.md)。

---

## 它做什麼

客戶輸入「我的退款呢？」之後，背後發生的事：

| 步驟 | 節點 | 決定什麼 |
|---|---|---|
| 1. 理解 | `dialogue_classifier` | 客戶想要什麼？情緒如何？只是閒聊嗎？ |
| 2. 風險檢查 | `risk_precheck` | 是否危險或敏感？是的話**停下來轉人工**。 |
| 3. 規劃查詢 | `evidence_planner` | 需要查什麼嗎（訂單狀態、政策）？ |
| 4. 取得資料 | `evidence_collector` | 從知識來源與工具取回。 |
| 5. 驗證資料 | `evidence_validator` | 只留下真的被要求的那些。 |
| 6. 選擇策略 | `strategy_selector` | 簡短、公事公辦，還是安撫？ |
| 7. 產生回覆 | `response_generator` | 寫出回答，並附上引用來源。 |
| 8. 事實查核 | `response_validator` | 第二個 AI 檢查回覆是否有依據。不合格就修復或轉人工。 |

每一步都會寫成一份 **trace**，含耗時、判斷結果，以及（Admin 才看得到）模型的
原始推理。Console 顯示的就是這些。

它也**記得對話**：步驟 1 和 7 會看到同一個 session 最近幾輪的內容，所以
「那要多久？」會接續前面談過的東西，而不是從頭問起。

**貫穿整份設計的唯一規則：**

> 助理只能陳述本回合實際取得的事實。

它無法自己發明一條退款政策。步驟 5 和 8 就是為了強制這件事而存在；當它們擋
下回覆時，轉人工是設計中的正確結果，不是失敗。

---

## 快速啟動

開始之前，先確認 **Docker Desktop** 已啟動，而且有一個 OpenAI-compatible 模型
服務（Ollama、vLLM 等）已經載入 `MODEL_CONFIG_PATH` 指向的檔案所指定的模型
（目前 Demo 設定是 `qwen3.6:27b`）。

**1. 執行腳本。** 第一次會把 `.env.example` 複製成 `.env`，然後停下來：

```bash
./run.sh
```

**2. 在 `.env` 填三個值：**

```dotenv
REMOTE_MODEL_BASE_URL=http://host.docker.internal:11434/v1
DEMO_CUSTOMER_TOKEN=至少16字元的隨機字串
DEMO_ADMIN_TOKEN=另一組至少16字元的隨機字串
```

模型跑在另一台機器時，把 `host.docker.internal` 換成該機器的 LAN 位址；模型
服務需要驗證時再加上 `REMOTE_MODEL_API_KEY`。**絕對不要提交 `.env`。**

**3. 再執行一次 `./run.sh`。** 它會建置 image、啟動 PostgreSQL + API + Worker、
跑 migration、載入 Demo 資料，並印出網址。

**4. 打開 http://localhost:8080/console/，貼上 *Admin* Token。** 送出
`good morning`，你應該會收到回覆，右側 trace 會一步一步填滿。如果沒有動靜，
先檢查相依服務：

```bash
curl http://localhost:8080/health/ready   # 檢查所有相依，含模型角色
./run.sh logs                             # 追蹤 API 與 Worker log
```

日常操作：

```bash
./run.sh stop     # 停止服務
./run.sh reset    # 停止服務，並永久刪除 Demo 資料庫
make restart      # 重新載入 config/*.yaml 的修改
```

沒有 Bash？把第 1、3 步手動做一次，其餘完全相同：

```powershell
Copy-Item .env.example .env
# 編輯 .env，然後：
docker compose up --build -d
docker compose --profile demo run --rm demo-seed
```

`docker compose down`、`down -v`、`restart app worker` 分別取代 stop、reset 與
`make restart`。

> ⏳ 回覆需要真實時間——單張 GPU 跑 27B 模型時，一則訊息大約 30 到 90 秒，因為
> 一個回合會呼叫模型好幾次。等待時看右側的步驟逐一完成，不要重送訊息。

不使用 Docker 直接在本機跑：見 [DEVELOPING.md](DEVELOPING.md)。

---

## 設定值

### 這三個沒設就啟動不了

`./run.sh` 缺少它們就會直接拒絕啟動，所以設錯會很明顯，不會變成難查的問題。

| 設定 | 要填什麼 | 規則 |
|---|---|---|
| `REMOTE_MODEL_BASE_URL` | 你的模型服務位址，含 `/v1` | `host.docker.internal` 是從 container 連回主機；模型在別台機器就填 LAN 位址。在 container 裡的 `localhost` 指的是 container 自己。 |
| `DEMO_CUSTOMER_TOKEN` | 任意隨機字串 | 至少 16 字元 |
| `DEMO_ADMIN_TOKEN` | **另一組**隨機字串 | 至少 16 字元。跟 customer token 相同的話，會被當成 customer，你永遠進不了 Tune 面板。 |

還有一件事必須成立，但**沒有任何程式會幫你檢查**：模型設定檔裡指定的模型必須
已經存在於那台伺服器上。目前提交的 `config/models.yaml` 要的是
`qwen3.6:27b`。用 `/health/ready` 確認——任何一個模型角色連不上，它就會失敗。

### 視情況才需要設

| 設定 | 什麼時候 |
|---|---|
| `REMOTE_MODEL_API_KEY` | 模型服務需要金鑰時。不需要就留空。 |
| `MODEL_CONFIG_PATH` | 你有多份模型設定檔時。改到**不是**生效中的那一份，什麼都不會變，也不會報錯——Tune → Models 會顯示目前生效的路徑。 |
| `DATABASE_URL` | 不用 Docker 直接跑時。Compose 會自己提供，並忽略這個值。 |
| `KNOWLEDGE_BASE_URL` | 要接真的知識庫，或不用 Docker 直接跑時（那時是 8000 port，不是 8080）。 |
| `LOCAL_VLLM_BASE_URL`、`LOCAL_VLLM_API_KEY` | 模型設定檔裡有 profile 指向本機 vLLM 時。 |
| `MODEL_TIMEOUT_SECONDS` | 模型很慢時。預設 90 秒適合託管模型；本機模型每秒只產生幾個 token 的話遠遠不夠，而且失敗會顯示成 `UNEXPECTED_ERROR`，不會說是逾時。 |

### 沒有特別理由就別動

| 設定 | 預設 | 作用 |
|---|---|---|
| `HISTORY_TURNS` | `8` | 助理看得到前面幾輪對話（0–40）。調高記得更多、也更耗 token；太高的話小模型會失焦。 |
| `ASSURANCE_MODE` | `bootstrap` | `bootstrap` = 一個查核者，`dual_judge` = 兩個且必須一致。 |
| `APP_RUNTIME_MODE` | `demo` | `production` 會拒絕 Demo Token，而在真正的驗證機制實作之前，那等於沒有任何入口。Compose 固定為 `demo`。 |
| `WEBHOOK_URL`、`WEBHOOK_SECRET` | stub | 轉人工的對話要送去哪。Demo 裡沒有任何服務在接。 |
| `DEMO_TENANT_ID`、`DEMO_CUSTOMER_ID` | `t1`、`c1` | Demo Token 以誰的身分行動。`config/demo/account.json` 有一份文件綁定 `c1`。 |
| `WORKER_OWNER` | `agent-flow-bootstrap` | Worker 在 job queue 裡的名字。 |

**行為不在 `.env` 裡。** 哪個模型做哪件事、助理知道什麼、語氣如何、每一步怎麼
判斷——全都在 `config/*.yaml`，由 [TUNING.md](TUNING.md) 說明。

---

## 使用 Console

```
┌───────────────┬──────────────────────┬─────────────────────┐
│  對話列表      │   對話視窗            │  這回合發生了什麼    │
│  (一個 chat id │   (在這裡輸入 —      │  (步驟即時長出；     │
│   一列；點擊    │    主要區域)         │   點一個看它的判斷)  │
│   可切換)      │                      │                     │
│───────────────│                      │                     │
│  回合列表      │                      │                     │
│  (所選對話的)  │                      │                     │
└───────────────┴──────────────────────┴─────────────────────┘
```

輸入訊息送出，右側 trace 會自動選取並即時填入；點任一步驟看它的判斷。值得
試試看的訊息：

| 輸入 | 展示什麼 |
|---|---|
| `good morning` | 正常回答，**完全不查**、不引用 |
| `where is my order order-1?` | 查訂單並引用工具結果（運送中） |
| `is it still on the way?` | 沿用上一輪的訂單——**對話記憶** |
| `where is my order order-2?` | 另一筆訂單，另一種狀態（已送達） |
| `how about order-3?` | 準備中——訂單編號只從這一句取得 |
| `how long do refunds take?` | 從知識來源回答並附引用 |
| `how long does shipping take?` | 換一份文件回答，不會誤用退款政策 |
| `八月團購有哪些咖啡可以選？` | 讀團購清單，報出品項與價格 |
| `這些品項的特色有啥？` | 由**另一份文件**回答——風味說明，不是再報一次價格 |
| `七月零食團購的取貨時間是幾點到幾點？` | 文件裡沒有取貨時間，所以它**直接說沒有** |
| `會員等級有哪些？點數怎麼算？` | 等級、門檻與點數規則，來自同一份文件 |
| `我有什麼折扣碼可以用嗎？` | 只綁定**這一位客戶**的文件 |
| `夏季特賣還有嗎？` | 促銷 **2025 年已過期**，因此看不見——不會報出過時內容 |
| `where is my refund?` | **轉交人工**——沒有可驗證的來源，拒絕猜測 |

其他該知道的：

- 左上的**對話列表**是一個 chat id 一列——就是 LINE webhook 會給的那個 id。
  點一個可以讀它的歷史並繼續聊。
- 重新整理頁面，對話還在。**New conversation** 開一個乾淨的。
- **EN / 中文** 切語言、**◐** 切明暗、**Retry trace** 重跑已完成的回合。
- **http://localhost:8080/docs** 是 API 文件，有 Authorize 按鈕——貼上同一組
  Admin Token 就能直接呼叫任何端點。

**Admin** 看得到全部：真實輸入輸出、模型原始推理、Tune 面板。
**Customer** 只看得到聊天。

---

## 調整它的行為

不用寫 Python。**[TUNING.md](TUNING.md)** 是完整指南，寫給調整助理的人，不是
寫給工程師。

| 我想改… | 改哪裡 |
|---|---|
| AI **知道什麼** | Console → **Tune** → Knowledge，或知識庫本身 |
| 它**聽起來怎樣** | Console → **Tune** → Voice，或 `config/personas/*.yaml` |
| 每一步**怎麼判斷** | Console → **Tune** → Instructions，或 `config/prompts/*.yaml` |
| 知識**從哪來** | `config/knowledge.yaml` |
| 客戶資料**查哪裡**（ERP、CRM） | `config/tools.yaml` |
| **哪個模型**做哪件事 | `MODEL_CONFIG_PATH` 指向的那個檔案 |
| 位址、Token、記憶長度 | `.env` |

原則是：**行為放 `config/`，機密與機器位址放 `.env`，服務拓撲放
`compose.yaml`。** 任何依機器而異的值都不該寫進 `Dockerfile`。

用 Admin Token 開啟 Tune，可以看到每一步目前的指令、語氣、助理可引用的文件，
以及每一步用哪個模型——都能即時修改，下一則訊息就生效。修改寫入
`config/overrides.json`（Git 忽略），不會改寫你的 YAML，所以 **Revert to file**
永遠有效。

---

## 系統結構

```
客戶訊息
      │
      ▼
┌─────────────┐    寫入 job      ┌──────────────┐    取走 job      ┌──────────────┐
│  FastAPI    │ ────────────────▶│  PostgreSQL  │ ────────────────▶│   Worker     │
│  (app)      │                  │  jobs +      │                  │  執行 8 步    │
│  提供 API    │ ◀────────────────│  traces      │ ◀────────────────│  pipeline    │
│  與 Console │    讀取 trace    │  (pgvector)  │    寫入 trace    │  呼叫模型     │
└─────────────┘                  └──────────────┘                  └──────────────┘
      │                                                                    │
      ▼                                                                    ▼
  瀏覽器 Console                                                        AI 模型
  (即時 trace)                                        (任何 OpenAI-compatible 服務)
```

**兩個程序，一個資料庫。** `app` 收訊息、提供 Console、讀出 trace，本身不呼叫
AI；`worker` 取走排隊中的 job、執行 pipeline，把每一步寫成 trace。
**沒有 message queue、沒有 Redis、沒有 Kafka**——job queue 就是一張 PostgreSQL
資料表。

```
src/agent_flow/
├── main.py            # Web app 組裝；提供 Console
├── runtime.py         # Demo 的 composition root——把所有東西接起來
├── worker.py          # 背景 job 執行器
├── contracts.py       # 各處共用的型別
├── observability.py   # trace / event / reasoning 如何被記錄
├── auth.py            # Demo Bearer Token 驗證
├── api/               # HTTP 端點（submissions、sessions、traces、config）
├── runtime_config.py  # 可編輯設定的即時檢視；Tune 面板的後端
├── pipeline/          # 大腦——八個步驟，一步一個檔案：
│   ├── classify.py    #   理解訊息
│   ├── risk.py        #   風險檢查
│   ├── evidence.py    #   規劃 / 取得 / 驗證資料
│   ├── respond.py     #   選策略、寫回覆、修復
│   ├── validate.py    #   事實查核
│   └── turn.py        #   依序執行 1→8 的指揮者
├── adapters/          # 對外：模型、知識來源、工具
├── repositories/      # 資料庫讀寫
└── console/           # Web UI（HTML/CSS/JS，中英 i18n）

config/                # 可編輯設定——不必重建，`make restart` 即可
├── models*.yaml       # 每一步用哪個模型（MODEL_CONFIG_PATH 決定用哪份）
├── knowledge.yaml     # AI 可以引用的所有知識來源
├── tools.yaml         # 查詢去哪：Demo fixture 或真實 ERP/CRM
├── prompts/           # 給每個 AI 步驟的指令
├── personas/          # 助理的語氣
└── demo/              # Demo 語料與工具的罐頭答案
```

技術組成：**Python 3.12**、FastAPI、Pydantic 2、PostgreSQL 16 + pgvector、
psycopg 3 + Alembic、uv、Docker Compose、pytest + Playwright。Console 是純
HTML/CSS/JS，不需要建置。

---

## 安全邊界與限制

- **只有 Demo 驗證。** `.env` 裡兩組靜態 Token。正式部署需要真正的驗證機制
  （`APP_RUNTIME_MODE=production` 會刻意拒絕 Demo Token）。
- **Reduced assurance。** 事實查核是規則加上一個 AI Judge，所以系統會把自己
  標示為 `reduced_assurance`，**不**適合無人監督執行。
- **小模型品質不穩。** 開放閒聊時出現較弱的回覆是預期內的，換大模型會改善。
- **Console 修改立即生效。** Tune 的修改下一則訊息就套用，沒有重啟、沒有
  code review。只有 Admin Token 進得去。
- **Admin 推理內容會被保存。** 模型原始推理寫進 trace，其他人看不到。用真實
  資料前要重新評估這個保存政策。
- **外部服務是必要條件。** 模型服務掛掉時 API 可能還活著，但 `/health/ready`
  與實際回覆都會失敗。
- **轉人工只是邊界，不是整合。** Webhook 接收端是 stub；真正的工單系統、告警
  與重試流程都還沒做。
- **沒有 LINE Adapter。** Submission API 已經是 channel-neutral、隨時可以接，
  但還沒有人寫——見 [DEVELOPING.md](DEVELOPING.md)。
- **Reset 不可逆。** `./run.sh reset` 與 `docker compose down -v` 會永久刪除
  Demo 資料庫。
- **絕不提交機密。** `.env`、真實 Token 與 API Key 只留在本機。

---

## 授權

**[MIT](LICENSE)。** 可自由使用、fork，並在上面繼續開發。

目前是 bootstrap demo runtime——用在任何真實場景之前，請先驗證與強化。
