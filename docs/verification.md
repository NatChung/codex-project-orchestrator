# v0.1.0 驗證紀錄

日期：2026-09-18。平台：macOS 26.4.1 / arm64；Python 3.13.15；Codex CLI 0.154.0。

## 已完成

| 項目 | 證據與結果 |
| --- | --- |
| 自動測試 | 43 項 unittest 通過；涵蓋設定、mode、rollback、mailbox、MCP role routing、worker lifecycle、RPC 與 doctor cleanup |
| 真實 sandbox | 三個角色、39 項檔案及連線檢查通過；預期拒絕均取得 EPERM，而非從文字指示推測 |
| 真實 worker 派收 | alpha、beta 各自讀取不同合成 fixture，回覆相同 task_id；從 app-server command evidence 核對 stdout 與 exit code；原檔案未變 |
| 收件流程 | worker task acknowledgement 與 orchestrator result acknowledgement 完成，兩側無未確認訊息 |
| MCP stdio | 實際啟動固定 orchestrator 身分，列出工具、送入 beta 任務、喚醒獨立 worker |
| 專案設定覆寫 | 合成 project config 試圖設定 full access；app-server config/read 確認該層因 untrusted 被略過，保留集中設定的 worker profile 與 network=false |
| 套件 | wheel／sdist 建置成功；wheel 安裝至另一個乾淨 virtualenv，從 repo 外成功執行 cpo --help |

Sandbox 39 項包含每個角色的自己／其他專案與 state 讀寫、`.codex`／`.git`／`.agents` 寫入，以及 loopback TCP 和私有 app socket 連線。Loopback 控制連線在 sandbox 外成功，避免將不存在的服務誤判為網路隔離。

## 測試中發現並處理

- Unix websocket 的壓縮協商與此版 app-server 不相容；client 明確停用壓縮。
- `approval_policy=never` 下的 MCP 工具需個別預先允許；只放行本套件固定介面。
- Dry-run 原先建立 lock file，現已改為完全不寫入並加入回歸測試。
- 服務啟動改為環境變數 allowlist，避免沿用任意 operator credentials。
- macOS temporary directory 的特殊規則使跨角色檔案存取未被拒絕；doctor 正確失敗。一般 home 目錄通過，派工要求同設定／同服務的成功 receipt。
- 另一個模型層跨專案測試中，worker 選擇不執行被禁止的讀取；該回覆只算 policy handling，沒有列為 OS denial 證據。

## 尚未涵蓋

- Linux／WSL 的實際 Codex sandbox、其他 Codex 版本、互動式 TUI 的完整人工驗收。
- 任意時點斷電或 crash 的自動復原，以及外部副作用的 exactly-once 執行。
- 公網目的地逐域限制；首版關閉 shell network，網路動態測試涵蓋 loopback TCP 與控制 socket。

每套安裝仍須執行 `cpo doctor --probe`。本次結果不替代新機器或新設定的實測；CI 的一般 Python 測試也不會冒充 OS sandbox 測試。
