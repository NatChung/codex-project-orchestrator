# 安全模型

Codex Project Orchestrator 的目標是在同一位可信 operator 的作業系統帳號內，縮小不同 Codex session 可見的檔案與工具範圍。它不是 hostile-user 隔離機制，也不是 multi-tenant security boundary。能以相同 OS user 執行任意程式的人，原則上也能讀取或修改該帳號可存取的資源。

## 信任與授權邊界

- Operator、套件安裝內容與啟動設定屬於 trusted computing base。
- Orchestrator 與 worker 接收的任務文字、repo 內容及 `AGENTS.md` 都可能是不可信輸入。它們可以提供工作指示，但不能自行授予外部通訊、發布、權限變更或跨 repo 存取。
- 註冊專案被標成 untrusted，以略過專案自己的 Codex config。Codex 仍會在任務中讀取 repo-local `AGENTS.md`；因此應將它視為指示資料，而不是 permission policy。
- MCP server 在 shell sandbox 之外執行。Shell 的檔案或網路限制不會自動套用到 MCP 實作，所以 adapter 必須只公開固定工具，並在 server 端綁定角色、驗證收件者與 task correlation，避免提供任意檔案或任意 SQL 介面。

`cpo ask` 與 operator MCP 是 operator 端的 persistent Orch 控制入口。兩者只透過 app server 對保存的 Orch thread 執行 turn；該 thread 仍套用 `orch` permission profile 與固定角色 `project_agents` MCP，不繼承呼叫端 Codex session 的檔案權限。Operator MCP 不公開 mailbox、任意 role 或任意 thread ID，只能 send、status、wait、read result、steer、interrupt，以及在人工核對後 acknowledge reconciliation。Operator-owned lock 序列化狀態變更；等待不持有 lock，因此仍可 steer 或 interrupt。

Orch thread ID、active turn ID、最後結果、控制面設定 fingerprint 與 reconciliation 狀態保存在 private runtime state。Fingerprint 包含 compiled config、thread 參數與 app-server service generation；每個新 service instance 都會取得新的隨機 generation token，sandbox probe receipt 也綁定同一 token。權限、MCP、thread 參數或 service instance 變更時，runtime 會先讀取舊 thread。只有舊 thread 明確停止且不需要 reconciliation 時才建立新 thread，並保存 previous thread ID。這也避免服務重啟後續接保留舊工具名稱、但沒有新 MCP binding 的 thread。新 thread 建立結果不確定時仍保留舊 thread 身分與 pending fingerprint。`turn/start` 後連線中斷屬於不確定操作，系統拒絕自動重送；status、steer 與 interrupt 都不能清除 reconciliation。Operator 必須先核對 mailbox、worker 與 thread 狀態，再留下 reconciliation note；這個 acknowledgement 只解除重送阻擋，不宣稱先前副作用不存在。解除後若遠端回合仍 active，runtime 會從 server state 恢復唯一 active turn ID；缺失、重複或與本機不一致時安全停止並再次要求 reconciliation。

## Runtime state 與工具面

Runtime state 由 operator 擁有，目錄應為 private，資料庫與敏感設定檔應只允許 owner 讀寫。各 Codex permission profile 會拒絕 session 直接讀寫 runtime state；session 只能經 role-bound adapter 使用必要操作。

每次部署使用專用 `CODEX_HOME`，不繼承 operator 日常環境中的 connectors、plugins 或 connector credentials。產生的設定會停用 plugins、multi-agent、web search 與 network proxy。若 operator 明確使用 `cpo login --reuse-current`，專用 home 會以 symbolic link 連到原本的 Codex file authentication；這是刻意選擇的 auth 共用例外，不代表 connectors 或 plugins 也被繼承。移除該 link 不會刪除原始 credential。這些措施可以縮小可用工具面，但不能把 MCP server 本身錯誤地視為已在 shell sandbox 內。

Mailbox 以參數化 SQL 與固定 schema 實作，不接受呼叫者提供任意 SQL。只有已註冊角色能收送；orchestrator 只能送給 worker，worker 只能回覆 orchestrator，而且回覆必須對應同一 worker 已收到的 task ID。只有收件者能 acknowledge。這些限制是訊息路由控制，並不構成不同 OS user 之間的機密性邊界。

`orchestrator`、`operator`、`orch` 與 `local` 是控制面保留身分，初始化與每次載入設定都拒絕將它們登錄為 worker。這項驗證必須同時涵蓋 CLI 輸入與 operator 手動修改的 TOML，避免 worker 透過名稱碰撞取得 operator 控制工具。

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

動態 worktree worker 不把執行中產生的新 profile 熱加到全域設定。初始化時會為每個 base project 編譯固定的 `worktree-<project>` profile；目前 worktree cwd 由 Codex 當作該回合的 workspace root。每次 worker turn 前，runtime 以 App Server `command/exec` probe 該回合即將使用的同一個 permission profile 與 cwd，通過後才把它傳給 `turn/start`。Probe 測試 worktree 與 Git metadata 可讀寫、base checkout／其他 worker／Orch workspace／state 不可讀寫、受保護路徑不可寫，以及 loopback TCP／app socket 不可連線。沒有 receipt 的靜態 base 設定或動態 probe 失敗時都拒絕派工。

Git linked worktree 的 `.git` 是指向 base repo metadata 的控制檔。為了讓 worker 可以 commit，動態 policy 必須允許寫自己的 worktree control directory 與共用 Git common directory；objects 與 refs 因而仍與 base checkout 及其他 linked worktree 共用。Source working tree 本身仍在 restricted read 範圍之外。這個邊界防止直接跨 working tree 讀寫，不能宣稱提供互相獨立的 Git object／ref storage，也不能阻止同一 repo 中 Git metadata 層面的互相影響。Shell network 維持關閉，因此 worker 不會自行 push。

macOS live 測試發現路徑相關限制：系統 temporary directory 內的測試專案及 state 出現跨角色存取仍被允許的結果，doctor 正確判為失敗。一般 home 專案目錄則通過檔案、保護目錄與連線拒絕檢查。建議使用一般 home 專案目錄，不要將控制資料或註冊專案放在系統 temporary directory。這份工具不能修正作業系統或 Codex 的 sandbox 差異；無法證明隔離時便停止派工。

## `local` 模式

`local` 模式刻意提供完整本機存取，只能透過明確的 CLI flag 選擇。它適合 operator 已接受完整存取風險的情況，不能描述成與 `isolated` 相同的安全邊界。

permission 或 mode 變更不是 hot reload。Operator 必須停止相關 app-server／session，再以新設定建立新 session。繼續使用舊 session 可能保留舊權限，不能用設定檔已更新來推定邊界已生效。

角色的 `model` 設定只選擇模型，不授予或撤銷任何權限。Worker 在建立／續接 thread 及啟動新回合時傳入設定的模型；活躍回合不會被中斷以套用新設定。直接在其他 Codex home 或桌面版啟動的對話不受本工具產生的 TOML 管理。

## 崩潰、重送與副作用

SQLite mailbox 讓完全相同的 send 冪等，並持久保存 acknowledgement，但不提供 exactly-once execution。Worker 可能已經完成外部副作用，卻在回覆或 acknowledge 前崩潰。此時狀態具有歧義，系統不應自動重試可能產生副作用的工作；operator 或 orchestrator 應先查核目標系統的實際狀態，再選擇最小的續作。

## 相容性與驗證範圍

初始測試作業系統為 macOS，Linux 尚未驗證。整合基準為 Codex `0.154.0`，app-server API 仍是實驗性介面。Codex、作業系統 sandbox 或 API 版本改變後，都應重新執行實際隔離測試，不能沿用舊版本結果。
