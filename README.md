# Codex Project Orchestrator

用一個協調入口，將任務交給不同專案的獨立 Codex worker。以 TOML 定義檔案與網路權限，以 SQLite 保存任務與回覆。

這是一個小型、可自行管理的本機工具。協調者負責派工與核對；每個 worker 有自己的 session、工作目錄與權限。專案本身的程式碼、業務規則與 Git 歷史留在各自的 repo。

## 目前範圍

- Python 3.11+、Codex CLI **0.154.0**；使用尚在變動的 app-server 與 permission profiles API。
- 首版驗證平台為 macOS。Linux／WSL 尚未驗證；原生 Windows 不支援本工具的 Unix socket 流程。
- 預設 `isolated`：只能寫自己的工作目錄，其他登錄專案與控制資料不可讀寫，shell 不開網路。
- `local`：操作者明確選用 full access，停用獨立 worker MCP。
- 不包含自動排程、開機服務、多使用者隔離，或跨崩潰的 exactly-once 執行保證。

## 安裝

先安裝並登入 Codex CLI，以及 Python 與 [uv](https://docs.astral.sh/uv/)。在專案外安裝工具，避免 worker 可以修改它：

```sh
uv tool install 'git+https://github.com/NatChung/codex-project-orchestrator.git@v0.1.0'
cpo --help
```

開發者可 clone 後使用 `uv sync --frozen`，再以 `uv run cpo` 執行。

## 開始使用

下面使用兩個已存在、互不包含的專案資料夾。協調 workspace 必須是空資料夾；初始化不修改專案的 `.codex/config.toml`、`AGENTS.md` 或 Git 設定。

```sh
cpo init --workspace ~/work/orchestrator \
  --project alpha=~/work/alpha \
  --project beta=~/work/beta
cpo login
cpo start
cpo doctor --probe
cpo orch
```

若要從另一個本機 session 交辦一次完整工作，不必控制既有的互動式終端機。`cpo ask` 會透過既有 app server 啟動或續接 persistent Orch thread，沿用相同的 `orch` 權限與 `project_agents` MCP，並在回合完成後把結果寫回目前的終端機：

```sh
cpo ask '請 kc-storefront worker 唯讀確認測試入口，附上路徑與執行證據。'
```

也可以從標準輸入提供 prompt：

```sh
cpo ask < task.txt
```

後續 `ask` 會續接同一個 Orch thread。Prompt 應包含目標、允許動作、限制、證據與完成條件；已有 active turn 時，新 prompt 會被拒絕，operator 可等待、steer 或 interrupt。

一般 Codex session 可安裝固定 operator MCP，讓模型直接控制 persistent Orch，而不取得 worker mailbox 或專案權限：

```sh
codex mcp add cpo_operator -- cpo mcp --role operator
```

重新開啟 Codex session 後會出現 `send_orchestrator_prompt`、`orchestrator_status`、`wait_orchestrator`、`read_orchestrator_result`、`steer_orchestrator`、`interrupt_orchestrator` 與 `acknowledge_orchestrator_reconciliation`。Operator MCP 只控制 Orch；專案派工仍由 Orch 經 `project_agents` 完成。

若既有 Codex 使用檔案儲存登入資訊，可用 `cpo login --reuse-current` 取代登入。它建立指向原有 credential file 的本機 symlink，不會顯示或複製 token；原登入更新也會生效。使用 keychain 的環境請在專用 home 重新登入。

初始化會將目前 Python runtime 加入唯讀例外，供診斷指令使用。其他工具鏈位於受限 home 內或非標準路徑時，在 init 加上精確的 `--runtime-read /absolute/toolchain/path`。這是所有角色共用的唯讀例外；不要指定整個 home、專案或 credential 目錄。`doctor --probe` 不通過時無法派工，請先檢查原因。建議使用一般 home 下的專案目錄；macOS 暫存目錄的特殊允許規則可能讓隔離檢查失敗。

在 Orch 對話中交辦，例如：

> 請 alpha worker 唯讀確認測試入口，beta worker 唯讀整理啟動步驟。各自附來源路徑及執行證據，禁止外部發送與修改。收齊後核對並回報。

尚無專案時，可先照 [兩個假專案示範](docs/demo.md) 試跑。

Orch 使用 `project_agents` MCP：`list_workers` → `send_message` → `check_worker_inbox` → `worker_status`／`fetch_inbox` → 核對保存 → `acknowledge_message`。送進信箱不等於開始執行；worker 回報也不等於人類驗收。

同一個 Git repo 需要平行工作時，Orch 可先呼叫 `create_worktree_worker(project, task_id, ref)`。工具會在 `worktree_root` 建立 detached linked worktree，回傳新的 `worker_id`；後續用該 ID 派工及喚醒。相同 project、task ID 與 ref 的重送是冪等的。需要一起測試、一起 commit 的跨 Flutter／React／backend 修改應放在同一個 worktree worker，不要依服務拆散。首版保留 worktree 供人工檢查，尚不自動刪除。

初始化會為每個 base project 預先產生一個 `worktree-<project>` permission profile。動態 worker 每回合都先以 App Server `command/exec` 對該回合即將使用的同一個 profile 與 worktree cwd 做實際檔案及網路 probe；失敗就不啟動模型。Profile 只允許寫目前 worktree workspace root 與 linked-worktree 必需的共用 Git metadata，拒絕 base checkout、其他 worker、Orch workspace 與 runtime state，並關閉 shell network。Linked worktree 仍共享 base repo 的 Git objects 與 refs，因此它是工作目錄隔離，不是互不影響的 Git storage。

## 管理設定

預設控制資料位於 `~/.local/share/codex-project-orchestrator/`。以 `cpo --state /path/to/private-state …` 可管理另一套獨立環境。資料夾不得與任何 worker 或 Orch workspace 重疊。

`settings.toml` 是操作者的來源設定；`codex-home/config.toml` 是產生的 Codex TOML。設定與執行環境分離，既有個人 connectors／plugins 不會自動匯入。登錄專案的 project config 層設為 untrusted，避免其覆寫集中管理的權限；worker 仍應讀自己的 `AGENTS.md`。

```sh
cpo status
cpo stop
# 關閉既有的 Orch／互動式 session，編輯 settings.toml 後：
cpo apply --dry-run
cpo apply
cpo start
cpo doctor --probe
cpo orch
```

新增專案需在 `settings.toml` 的 `[workers.<id>]` 設定 `cwd` 與 `profile = "worker-<id>"`。專案 ID 使用小寫字母、數字與連字號，開頭須為字母。

`worktree_root` 預設是 Orch workspace 同層的 `worktrees` 資料夾，可在 `settings.toml` 指定另一個不與專案、state 或 credential 目錄重疊的 canonical absolute path。變更後同樣要 apply、重啟並重新 probe。

模型按角色指定：Orch 預設 `gpt-6-astra`，所有 worker 預設 `gpt-5.6-sol`。
初始化會將 `model` 寫入各角色設定；舊設定省略此欄位時也使用上述預設。
可在既有角色區段內調整，例如：

```toml
[orchestrator]
cwd = "/synthetic/orch"
profile = "orch"
model = "gpt-6-astra"

[workers.alpha]
cwd = "/synthetic/alpha"
profile = "worker-alpha"
model = "gpt-5.6-sol"
```

Orch 從專用 Codex home 的設定讀取模型；worker 在 thread start／resume 與每次 turn start 明確指定模型。
變更後依上述 stop → apply → start → doctor → orch 流程重啟；正在執行的回合不會切換模型。
帳號及 Codex 版本必須支援指定模型。本工具不會自動替換不可用的模型。
這些設定只作用於本工具啟動的 session；直接在 Codex 桌面版開啟本 repo 的對話不會自動套用。

```sh
cpo stop
cpo mode local --allow-full-access
cpo orch
# 回到隔離模式，先結束舊 session：
cpo mode isolated
cpo start
cpo doctor --probe
cpo orch
```

模式切換不會熱更新舊 session，也不會撤銷已完成的修改。`stop` 拒絕停止活躍 worker；`stop --force` 可能留下執行結果不明的任務，必須先核對實際副作用再決定續作。

## 測試與安全邊界

```sh
uv run python -m unittest discover -s tests -v
```

單元／整合測試不呼叫模型。`doctor --probe` 會在登錄資料夾建立唯一命名的暫時測試檔，透過真正 sandbox 測試後清除，不修改既有檔案。完整 worker 回合另會使用 Codex 帳號額度。

這套工具信任本機操作者與安裝的套件。TOML 約束 sandbox 指令；MCP server 在 sandbox 外執行，須由固定角色介面另行限制。詳細說明見 [架構](docs/architecture.md)、[安全邊界](docs/security.md) 與 [驗證紀錄](docs/verification.md)。

MIT License。此專案為社群工具，非 OpenAI 官方產品。
