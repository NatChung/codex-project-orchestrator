# 安全模型

Codex Project Orchestrator 的目標是在同一位可信 operator 的作業系統帳號內，縮小不同 Codex session 可見的檔案與工具範圍。它不是 hostile-user 隔離機制，也不是 multi-tenant security boundary。能以相同 OS user 執行任意程式的人，原則上也能讀取或修改該帳號可存取的資源。

## 信任與授權邊界

- Operator、套件安裝內容與啟動設定屬於 trusted computing base。
- Orchestrator 與 worker 接收的任務文字、repo 內容及 `AGENTS.md` 都可能是不可信輸入。它們可以提供工作指示，但不能自行授予外部通訊、發布、權限變更或跨 repo 存取。
- 註冊專案被標成 untrusted，以略過專案自己的 Codex config。Codex 仍會在任務中讀取 repo-local `AGENTS.md`；因此應將它視為指示資料，而不是 permission policy。
- MCP server 在 shell sandbox 之外執行。Shell 的檔案或網路限制不會自動套用到 MCP 實作，所以 adapter 必須只公開固定工具，並在 server 端綁定角色、驗證收件者與 task correlation，避免提供任意檔案或任意 SQL 介面。

## Runtime state 與工具面

Runtime state 由 operator 擁有，目錄應為 private，資料庫與敏感設定檔應只允許 owner 讀寫。各 Codex permission profile 會拒絕 session 直接讀寫 runtime state；session 只能經 role-bound adapter 使用必要操作。

每次部署使用專用 `CODEX_HOME`，不繼承 operator 日常環境中的 connectors、plugins 或 connector credentials。產生的設定會停用 plugins、multi-agent、web search 與 network proxy。若 operator 明確使用 `cpo login --reuse-current`，專用 home 會以 symbolic link 連到原本的 Codex file authentication；這是刻意選擇的 auth 共用例外，不代表 connectors 或 plugins 也被繼承。移除該 link 不會刪除原始 credential。這些措施可以縮小可用工具面，但不能把 MCP server 本身錯誤地視為已在 shell sandbox 內。

Mailbox 以參數化 SQL 與固定 schema 實作，不接受呼叫者提供任意 SQL。只有已註冊角色能收送；orchestrator 只能送給 worker，worker 只能回覆 orchestrator，而且回覆必須對應同一 worker 已收到的 task ID。只有收件者能 acknowledge。這些限制是訊息路由控制，並不構成不同 OS user 之間的機密性邊界。

## `isolated` 模式

預設 `isolated` 模式的設計邊界如下：

- shell network 關閉；
- 每個角色只對自己的 repo 有一般寫入權限；
- 自己 repo 內的 `.codex/`、`.git/`、`.agents/` 降為唯讀；
- 其他已註冊 repo 一律拒絕；
- runtime state 與使用者 home 一律拒絕；
- 初始化時會自動將執行本套件所需的 Python `sys.base_prefix` 加入唯讀 runtime exception；operator 也可以明確加入其他 `runtime_read` 路徑。除此之外的 runtime dependency 不會自動開放。

每套安裝必須以真實 Codex sandbox 做正向與反向測試：自己 repo 可寫、受保護子目錄不可寫、其他 repo 與 state 不可讀寫，以及 shell 的 loopback TCP／私有 app socket 連線遭拒。只有 `EPERM`／`EACCES` 算拒絕證據，連線逾時或拒絕連線不足以證明 sandbox 生效。實際首版結果見 [驗證紀錄](verification.md)。

成功的 doctor probe 會寫入綁定 compiled config SHA-256 與目前 service PID 的 receipt。Orchestrator 在喚醒 worker 前會核對 receipt、現行 compiled config 與 service；receipt 缺失、失敗、設定不符或 service 已更換時拒絕 dispatch。Receipt 只證明該次 probe 實際涵蓋且通過的項目，不能延伸成未測安全性質的證明。

macOS live 測試發現路徑相關限制：系統 temporary directory 內的測試專案及 state 出現跨角色存取仍被允許的結果，doctor 正確判為失敗。一般 home 專案目錄則通過檔案、保護目錄與連線拒絕檢查。建議使用一般 home 專案目錄，不要將控制資料或註冊專案放在系統 temporary directory。這份工具不能修正作業系統或 Codex 的 sandbox 差異；無法證明隔離時便停止派工。

## `local` 模式

`local` 模式刻意提供完整本機存取，只能透過明確的 CLI flag 選擇。它適合 operator 已接受完整存取風險的情況，不能描述成與 `isolated` 相同的安全邊界。

permission 或 mode 變更不是 hot reload。Operator 必須停止相關 app-server／session，再以新設定建立新 session。繼續使用舊 session 可能保留舊權限，不能用設定檔已更新來推定邊界已生效。

## 崩潰、重送與副作用

SQLite mailbox 讓完全相同的 send 冪等，並持久保存 acknowledgement，但不提供 exactly-once execution。Worker 可能已經完成外部副作用，卻在回覆或 acknowledge 前崩潰。此時狀態具有歧義，系統不應自動重試可能產生副作用的工作；operator 或 orchestrator 應先查核目標系統的實際狀態，再選擇最小的續作。

## 相容性與驗證範圍

初始測試作業系統為 macOS，Linux 尚未驗證。整合基準為 Codex `0.154.0`，app-server API 仍是實驗性介面。Codex、作業系統 sandbox 或 API 版本改變後，都應重新執行實際隔離測試，不能沿用舊版本結果。
