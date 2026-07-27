# 開發 Tracewright

[English](DEVELOPING.md) | [繁體中文](DEVELOPING.zh-TW.md)

寫給下一位接手的人。假設你已經讀過 README，也能把 Demo 跑起來。這裡寫的都是
程式碼自己不會告訴你的事。

---

## 不用 Docker 直接跑

需要 Python 3.12、[uv](https://docs.astral.sh/uv/)、可連線的 PostgreSQL，以及
模型服務。`./run.sh` 是 Docker 版本，也是 Demo 實際跑的方式。

```bash
cp .env.example .env      # 然後把 DATABASE_URL 改成主機連得到的位址
uv sync --frozen
uv run --frozen alembic upgrade head
uv run --frozen python -m agent_flow.seed_demo

export KNOWLEDGE_BASE_URL=http://localhost:8000/mock-kb   # 兩個終端都要
uv run --frozen uvicorn --factory agent_flow.runtime:create_runtime_app --reload
uv run --frozen python -m agent_flow.worker --run         # 第二個終端
```

`KNOWLEDGE_BASE_URL` 很重要，因為 Demo 的知識庫是 app 自己提供的：在 Compose
上是 `http://app:8080/mock-kb`，在本機是 8000 port。設錯的話，檢索會**安靜地**
什麼都查不到。本機 Console 在 `http://localhost:8000/console/`。

**沒人告訴你就會浪費一小時的一件事：** `src/` 是烤進 image 的，`config/` 是
bind-mount。所以改程式碼要 `docker compose up -d --build app worker`，改設定只
要 `docker compose restart app worker`——而 artifact 在啟動時載入，所以改設定
確實需要那個 restart。

`DATABASE_URL`（必須是 container 主機名稱 `postgres`）與
`APP_RUNTIME_MODE=demo` 由 `compose.yaml` 掌握。在 `.env` 設這兩個值只會影響
本機直跑。

## 測試

六個目錄，成本並不相同。

| 目錄 | 需要 | 時間 | 何時跑 |
|---|---|---|---|
| `tests/unit` | 無 | 約 5 秒 | 一律 |
| `tests/contract` | 無 | 含在上面 | 一律 |
| `tests/browser` | Playwright 瀏覽器 | 約 15 秒 | 動到 `console/` 時 |
| `tests/integration` | PostgreSQL | 約 30 秒 | 動到 `repositories/` 或 migration 時 |
| `tests/e2e` | Docker Compose | 數分鐘 | 發布前 |
| `tests/live` | 真實模型服務 | 數分鐘，會消耗 token | 改 prompt 或 gateway 時 |

日常指令是：

```bash
uv run pytest tests/unit tests/contract -q      # 約 300 個測試，不需要任何服務
```

`uv run pytest` 不帶參數會嘗試跑全部六個，在沒有 Postgres 和模型服務的機器上
一定會失敗。那不是 checkout 壞掉。

**新增 browser 測試時，請使用 `authenticate()` helper，不要自己重寫一份。**
登入會還原對話、載入 trace，然後把游標放進輸入框——這些發生在點擊回傳之後好幾
個 await。測試如果在這之前就開始操作頁面，focus 會在中途被搶走。Helper 會等
輸入框取得 focus，那是 app 自己「登入已穩定」的訊號；跳過這個等待，正是先前
兩個測試每四次跑就掛一次的原因。

## 一則訊息如何變成回覆

```
POST /api/v1/submissions
   └─ app 寫一列到 jobs 表，立刻回 202
        └─ worker 租用該 job，執行 8 節點 pipeline，寫入 span
             └─ console 輪詢 /submissions/{id} 直到終態
```

兩個程序、一個資料庫、沒有 message broker。**這件事對程式碼的影響大於其他任何
決定**——見下面的「兩個程序，一個資料庫」。

Pipeline 在 `src/agent_flow/pipeline/turn.py`，那是第一個該讀的檔案。它依序
呼叫：`classify` → `risk` → `evidence`（規劃、取得、驗證）→ `respond`（策略、
產生）→ `validate` → 修復或轉人工。

## 其他一切都在服務的那條不變式

**助理只能陳述本回合實際取得的事實。**

三個機制在強制它，改動其中一個之前，要先理解另外兩個：

1. **分類器只能點名它在 catalog 裡看過的來源**
   （`adapters/evidence.py::catalog`）。它無法發明一個文件 id。這就是
   `plan_evidence` 用 `source_id` 查詢、而不是用客戶原句查詢的原因。
2. **確定性的 hard failure**（`pipeline/validate.py::_hard_failures`）——與取得
   的證據對不上的引用、沒有來源的價格、送達承諾、行動承諾。這些是 regex 層級
   的檢查，跑在任何模型之前。
3. **一個 AI Judge** 拿草稿去比對證據
   （`config/prompts/response_judge.v1.yaml`）。

第 2 或第 3 沒過就進入修復，再不行就轉人工。轉人工是這個設計的**成功**，不是
錯誤——另一個選項是編一個答案出來。

因為一個 judge 不等於兩個，系統會把自己的信心標為 `reduced_assurance`。
`ASSURANCE_MODE=dual_judge` 是更嚴格的路徑。

## 兩個程序，一個資料庫

API 跑在 `app`，pipeline 跑在 `worker`。它們只共用 Postgres。

每一個「即時編輯」功能都必須跨過那條界線，而 **bug 就在那裡**。一個真實案例：
Tune 面板在 `app` 寫入 prompt override，`worker` 卻抱著開機時載入的 prompt，
於是 Console 的修改被安靜忽略，API 卻回報已套用。現在
`RuntimeConfigService` 和 `KnowledgeSources` 都會在檔案變動時重新載入。

**如果你新增另一個可即時編輯的東西，它需要同樣的處理，以及一個用兩個 service
實例操作同一個目錄的測試。** 所有在單一程序裡建立單一物件的既有測試，在功能
壞掉時都還是會通過。

## 知識實際上從哪來

主要語料放在**外部知識庫**，透過 HTTP 取得：`catalog_url` 列出它有哪些內容，
`document_url` 取回其中一份。那份清單就是分類器看到的東西，而它只能點名清單裡
出現過的來源——所以「有什麼」由知識庫決定，上面那條不變式依然成立。

`api/mock_kb.py` 是替代品，用同一個介面提供已提交的 Demo 語料，所以 Demo 不需
要任何外部服務。它是假的，不是功能：把 `KNOWLEDGE_BASE_URL` 指向真的知識庫，
然後刪掉那個 router。

知識庫不該持有的東西——綁定單一客戶的文件、tuner 從 Console 編輯的項目——留在
本地 `fixture` 來源。過期與客戶範圍屬於語料擁有者的責任，所以 mock 兩者都實作
了；真的知識庫也應該這樣做。

## 如何擴充

以下每一項都是「一個 registry 加一個設定檔」，都不會動到 pipeline。

| 要新增 | 註冊在 | 宣告在 |
|---|---|---|
| 知識後端（向量資料庫、其他 API 形狀） | `adapters/knowledge.py::_BUILDERS` | `config/knowledge.yaml`——內附 `fixture` 與 `http` |
| 工具後端（其他 ERP/CRM 形狀） | `adapters/tools.py::_BUILDERS` | `config/tools.yaml`——內附 `fixture` 與 `http` |
| 模型供應商 | `config/models*.yaml`——profile 就是資料 | — |
| 通道（LINE、網頁 widget） | 把 webhook 轉成 `POST /api/v1/submissions` | — |

通道這一項值得強調：submission API 是 channel-neutral，`session_id` 就是該通道
提供的 chat id。LINE adapter 完全不需要改 pipeline。

**新增一個 pipeline 節點**是唯一不屬於設定的事。你要把它加進
`pipeline/turn.py`、在 `config/prompts/` 給它一份 prompt artifact，並把它的輸出
contract 加到 `pipeline/model_outputs.py`。節點就是接受型別化輸入的一般 async
函式；沒有 plugin 系統，而且是刻意的。

## 值得保留的慣例

- **每一個模型輸出都是 Pydantic contract**（`contracts.py`、
  `pipeline/model_outputs.py`）。不讓鬆散的 dict 跨越節點邊界。小模型回傳垃圾
  的頻率高到讓這件事是承重的。
- **錯誤以安全代碼呈現，絕不外露內部細節。** `WORKER_LEASE_EXPIRED`、
  `TOOL_TIMEOUT`、`MODEL_CAPABILITY_FAILED`。Readiness 的輸出絕不能帶模型輸出
  或例外內容——上游 URL 的 query string 裡可能藏著金鑰。
- **範圍來自 token，絕不來自 request body。** `bind_customer_context` 是取得
  `AuthorizedCustomerContext` 的唯一方式。
- **註解解釋為什麼，不解釋做什麼。** 這個 repo 的多數註解記錄的是真的踩過的
  bug。要刪掉一則之前，先確認那個 bug 不會回來。
- **Prompt 是有版本的 artifact。** 改文字就加 `version`；checksum 會落在 span
  上，所以 trace 永遠能指認是什麼產生了它。

## 刻意沒做的東西

不是遺漏，是決定。要重開這些議題，請睜大眼睛。

| 沒做 | 為什麼 |
|---|---|
| 用 SSE 或 WebSocket 推 trace | Console 是除錯工具，輪詢沒有造成可量測的傷害，而且 `EventSource` 無法送 `Authorization` header——那會需要一個 ticket 端點。 |
| Message broker | Job queue 就是一張 Postgres 表。少跑一個服務，而且 trace 存在同一個資料庫。 |
| 真正的驗證機制 | 兩組靜態 Demo Token。`APP_RUNTIME_MODE=production` 會拒絕它們，那就是把這件事做對的掛勾點。 |
| 用 API 改模型設定 | 它帶有 endpoint URL 並選擇憑證；不該讓一組 admin token 就能把模型角色指向它自己選的伺服器。 |
| 對客戶原句做語意 RAG | 檢索被刻意限制在 catalog 的 source id——見上面的不變式。要改這個，得先回答「分類器要如何才不會發明文件」。 |
| Embedding／pgvector 檢索 | 接好但停用：Demo 從 fixture 回答，而且程式碼預期 1024 維。 |
| CI | 沒人建。`tests/unit tests/contract` 不需要任何服務，五分鐘就能寫完一個 workflow。 |

## 值得知道的營運細節

**API。** 範圍一律由 bearer token 綁定，所以沒有任何 request body 帶
`customer_id`。

| 端點 | 作用 |
|---|---|
| `POST /api/v1/submissions` | 排入一個回合：`{channel, external_message_id, session_id, text, idempotency_key}`，回 202。 |
| `GET /api/v1/submissions/{id}` | 輪詢直到終態。 |
| `GET /api/v1/traces/{id}` | 整份 trace，逐節點。 |
| `GET /api/v1/sessions` | 該 token 看得到的對話，最新在前。 |
| `GET /api/v1/sessions/{id}/messages` | 重播對話紀錄——Console 重新載入時走這條。 |
| `GET/PUT/DELETE /api/v1/config/...` | 即時的 prompt、persona、knowledge（Admin）。 |

`/docs` 是完整的互動式文件，有 Authorize 按鈕。

**記憶**以 `session_id` 為單位、標註角色，並限制在 `HISTORY_TURNS`（預設 8）
輪。不設上限的歷史曾經超過分類器的 100 則訊息上限，直接讓整個回合失敗。轉人工
的回合也會保留，所以客戶在轉人工之後換句話問，不會從零開始。

**失敗以安全代碼呈現**，絕不外露內部細節：租約逾時會以
`WORKER_LEASE_EXPIRED` 結束 trace 並保留一個重試 trace；工具逾時則是元件
`order_api` 上的 `TOOL_TIMEOUT`。完整的鏈是
`readiness check → model role → probe stage → trace node → component →
operation → error code → retry disposition`。

**每個模型 profile 的 `structured_output`** 決定 JSON 如何被強制。Ollama 的
`/v1` 接受 OpenAI 的 `json_schema` 欄位然後忽略它，所以 Ollama profile 必須用
`json_object`；vLLM、TGI 與 OpenAI API 支援 `json_schema`。無論哪種，schema 都
會在 system prompt 裡再寫一次。

## 交給別人之前

- [ ] `.env` 仍然被 Git 忽略，而且沒有任何 token、金鑰或內網位址流進受版控的
      檔案、截圖或 log。
- [ ] 下一位維護者知道模型服務在哪、`MODEL_CONFIG_PATH` 選的是哪些模型名稱——
      而且真的拿得到那個檔案（它可能是被 Git 忽略的本機設定）。
- [ ] `docker compose config --quiet` 沒有錯誤，Demo 能從空資料庫啟動，
      `/health/ready` 通過。
- [ ] 用 Admin Token 實際送一則訊息走完全程——API、worker、模型、知識、工具、
      trace。
- [ ] 說明 `config/overrides.json` 是否存在，以及哪些 Console 的修改還需要回寫
      進 YAML。
- [ ] 說明 `./run.sh reset` 與 `docker compose down -v` 不可逆。

## 從哪裡開始讀

1. `pipeline/turn.py` — 整個流程在一個檔案裡
2. `contracts.py` — 到處傳遞的資料形狀
3. `pipeline/evidence.py` — 檢索，以及運作中的不變式
4. `pipeline/validate.py` — 什麼擋下了錯誤答案
5. `runtime.py` — 真實物件如何被接起來

然後在 Console 挑一份 trace，跟著它走過這五個檔案。那是把整個系統裝進腦袋最快
的方法。
