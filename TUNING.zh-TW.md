# 調校指南

[English](TUNING.md) | [繁體中文](TUNING.zh-TW.md)

寫給決定助理怎麼表現的人。**不用寫 Python。** 多數事情在 Console 就能做完，
其餘的就是改一個文字檔。

---

## 從這裡開始：你想改什麼？

| 我想改… | 在哪裡 | 何時生效 |
|---|---|---|
| 助理**知道什麼** | Console → **Tune** → Knowledge | 下一則訊息 |
| 它**聽起來怎樣** | Console → **Tune** → Voice，或 `config/personas/*.yaml` | 下一則訊息／重啟 |
| 每一步**怎麼判斷** | Console → **Tune** → Instructions，或 `config/prompts/*.yaml` | 下一則訊息／重啟 |
| 它**去哪裡查東西** | `config/knowledge.yaml`、`config/tools.yaml` | 重啟 |
| **哪個模型**做哪件事 | 模型設定檔——見 §5，不一定是 `models.yaml` | 重啟 |
| 記憶長度、assurance 模式 | `.env` | 重啟 |

「重啟」是指 `make restart`。它會重讀 `config/`，不會重新建置。

---

## 1. 助理知道什麼——Knowledge

助理只能陳述它實際取得的事實。它被允許講的每一句話都來自一份**知識文件**，
而且每個回答都會標示引用了哪一份。所以「助理答錯了」通常是「那份文件寫錯了」，
要去文件所在的地方修。

**兩種來源，改哪一種很重要：**

- **知識庫**（`type: http`）存放主要語料。你要在知識庫本身用團隊既有的工具
  編輯。Demo 內附一個替代品，所以開箱就能跑。從這裡看它是唯讀的——Tune 面板
  會標示 *Read-only* 且不讓你修改。
- **本地來源**（`type: fixture`）存放細項：綁定單一客戶的文件，以及你想在這裡
  直接改的內容。

**從 Console 操作：** Tune → Knowledge。每個來源會列出它的文件。可編輯的來源
可以就地修改、刪除，或用最下方的表單新增。下一則訊息就生效，不必重啟。

**動手前先讀這段。** 這裡的文件會被當成**真的**。事實查核步驟比對的是回覆與
取得的文件，不是回覆與現實。你打進去的內容會被當成事實、附上引用告訴客戶。
這既是它的威力，也是它的危險。

**一份好文件**用客戶會用的說法，把一個問題完整回答完：

```
source_id: groupbuy:tea-2026-09
content:   九月茶葉團購清單：高山烏龍四兩 NT$520、蜜香紅茶四兩 NT$460。
           訂購截止 2026-09-05，9/12 於台北辦公室三樓取貨，取貨時間 10:00-18:00。
```

`source_id` 是標籤。用前綴分群——`policy:`、`groupbuy:`、`account:`——因為語料
變多之後你會靠它掃視，而且引用時顯示的就是它。

**文件怎麼切，比你想的更重要。** 如果一份文件同時放了價目表和口味說明，客戶
問口味時，助理只能說不知道，或是把價格套在錯誤的標題下丟回來。Demo 裡
`groupbuy:coffee-2026-08` 和 `groupbuy:coffee-2026-08:notes` 分成兩份就是這個
原因。**一個問題，一份文件。**

**兩件只能在檔案裡做的事**（`config/demo/*.json`）：

- `customer_id: c1` — 只有該客戶能取得。見 Demo 的 `account:c1:coupon`。
- `valid_until: "2025-09-01T00:00:00Z"` — 文件過期。之後它完全隱形：不會被取得，
  也不會出現在給分類器的清單裡。促銷用這個，不要用刪除。

### 新增一整個來源

`config/knowledge.yaml` 列出所有來源。新增一個只要三行：

```yaml
sources:
  faq:
    type: fixture
    path: config/demo/faq.json
```

`enabled: false` 可以先停用一個來源而不刪掉它。

## 2. 查詢單一客戶的資料——Tools

Knowledge 是大家共用的內容。**Tool** 回答關於某一位客戶的問題，而且帶參數：
「order-3 在哪裡」。設定在 `config/tools.yaml`：

```yaml
tools:
  order.lookup:
    type: fixture               # Demo 的罐頭答案
    path: config/demo/tools.json
```

不用改任何程式碼就能換成真的 ERP 或 CRM——把 `type` 改成 `http` 並給一個 URL。
`config/tools.yaml` 裡註解掉的那段就是可用的範例。注意其中的 `map:`：
**只有列在那裡的欄位才會到達客戶**，所以它同時也是把內部欄位擋在回覆之外的
方法。

## 3. 它聽起來怎樣——`config/personas/`

Persona 是語氣。它只改變用字：永遠不能覆寫政策、隱藏已查到的事實，或改變安全
判斷。檔案最下方的 `guardrails` 區塊就是強制這件事的地方。

```yaml
applies_to:            # 哪些對話類型使用這個語氣
  - emotional_support
  - casual

style_prompt: |
  Speak like a familiar friend, not a support agent reading a script.
  Be brief but concrete. Two or three sentences is usually enough.
```

**新增第二種語氣：** 複製檔案，換一個 `artifact_id` 和不同的 `applies_to`，
放進資料夾即可，會自動被載入。一種對話類型最多只能被一個 persona 認領——兩個
persona 都寫 `casual` 會在啟動時報錯。

可用的對話類型：`informational`、`transactional_read`、`complaint`、
`emotional_support`、`casual`、`boundary`、`unknown`。

一個 persona 只服務一種語言。`zh-TW` 的語氣不會套用在用英文提問的客戶身上。

## 4. 每一步怎麼判斷——`config/prompts/`

一個步驟一個檔案。`system_prompt` 是白話文，會當成 system message 送給模型。

| 檔案 | 控制什麼 |
|---|---|
| `dialogue_classifier.v1.yaml` | 判斷客戶要什麼，以及是否需要查資料 |
| `strategy_selector.v1.yaml` | 選擇回覆的形式 |
| `response_generator.v1.yaml` | 寫回覆，以及 grounding 規則 |
| `response_judge.v1.yaml` | 送出前的事實查核 |

有實質修改就把 `version` 往上加。每一份 trace 都會記下版本與當時實際執行的
文字指紋，所以你永遠能分辨是哪個版本產生了哪個回答。

這些檔案裡 `system_prompt` 以下的部分是機器檢查的結構。除非你同時改程式碼，
否則不要動。

**Judge 是兩面刃。** 把 `response_judge` 收緊可以擋掉捏造，但收得太緊會開始
否決正確的回答，讓該回合以轉人工收場。如果你改完它之後轉人工突然變多，原因
就在這裡。Prompt 裡兩半都要寫：什麼必須被擋下，以及什麼必須仍然通過的例子。

## 5. 哪個模型——模型設定檔

**改任何東西之前，先確認哪一個檔案是活的。** `config/` 底下有好幾個
`models*.yaml`，由 `.env` 裡的 `MODEL_CONFIG_PATH` 決定哪一個生效。改到不生效
的那個，什麼都不會變，也不會有錯誤訊息。

Console 會告訴你：Tune → Models 的表格下方就是目前生效的路徑。

```yaml
profiles:
  local_qwen:
    model: qwen3.6:27b      # ← 你的模型服務上的模型名稱
    temperature: 0          # 0 = 每次都給一樣的答案
    structured_output: json_object

roles:
  dialogue_classifier: local_qwen
  response_generator: local_qwen   # ← 想的話可以指向更大的 profile
```

**只讓最終回覆用大模型：** 新增第二個 profile，然後改 `roles` 底下那一行。

`structured_output` 很關鍵：Ollama 的 `/v1` 接受 OpenAI 的 `json_schema` 欄位
然後忽略它，所以 Ollama 的 profile 必須用 `json_object`。vLLM、TGI 與 OpenAI
API 支援 `json_schema`。

模型服務的位址不在這個檔案裡——它是 `.env` 的 `REMOTE_MODEL_BASE_URL`，所以
模型設定檔可以安全地提交到版控。

## 6. 設定值——`.env`

| 設定 | 作用 |
|---|---|
| `MODEL_CONFIG_PATH` | 哪一份模型設定檔生效 |
| `REMOTE_MODEL_BASE_URL` | 模型服務在哪 |
| `DEMO_CUSTOMER_TOKEN` / `DEMO_ADMIN_TOKEN` | Console 密碼。Admin 另外解鎖 Tune 與完整推理 |
| `HISTORY_TURNS` | 助理看得到前面幾輪對話（預設 8） |
| `ASSURANCE_MODE` | `bootstrap` = 一個查核者，`dual_judge` = 兩個且必須一致 |

---

## 在 Console 編輯

**Tune** 面板顯示的是「現在正在跑的東西」——每一步的指令、語氣、知識，以及每
一步用哪個模型。

在那裡儲存**不會**改寫 YAML 檔。它會疊上一層 override，存在
`config/overrides.json`，所以你寫滿註解的設定檔保持原樣，**Revert to file**
永遠有效。面板會把目前被覆寫的項目標成 *Edited here*。

要讓修改變成永久的，就把它寫進 YAML，然後把 override 還原。

Knowledge 是例外：它會直接寫進來源指向的 JSON 檔，所以在那裡新增的文件是對
那個檔案的真實修改。

## 確認它真的生效了

每次修改都會改變 artifact 的指紋，而那個指紋會記錄在每一個用到它的回合的每一
步上。送一則訊息、打開 trace，該步驟就會顯示它用的版本——所以修改前後的回覆
永遠分得出來。

如果回覆看起來沒變，先看指紋再決定要不要繼續改。指紋相同表示你的修改沒有進到
pipeline；指紋不同表示進去了，該調的是用字。

## 答案不對時，先看 trace

Console 會顯示每一步。「它為什麼這樣說」的答案，幾乎都看得見：

| trace 裡看到的 | 意思 | 去哪裡修 |
|---|---|---|
| 什麼都沒取得 | 分類器認為不需要查，或找不到符合的來源 | `dialogue_classifier` prompt，或文件的用字——它必須能從問題被認出來 |
| 取得了錯的文件 | 兩份文件在分類器眼中太像 | 拆開它們，或讓 `source_id` 和第一行更有區別 |
| 文件對，答案錯 | 產生器讀錯了 | `response_generator` prompt |
| 轉交人工 | Judge 否決了回覆，或風險步驟擋下 | trace 會指出是哪一條標準沒過 |
| 回覆講了沒有任何文件講過的事 | 這不該發生——grounding 失效 | 回報它；那是 bug，不是調校問題 |
