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

若既有 Codex 使用檔案儲存登入資訊，可用 `cpo login --reuse-current` 取代登入。它建立指向原有 credential file 的本機 symlink，不會顯示或複製 token；原登入更新也會生效。使用 keychain 的環境請在專用 home 重新登入。

初始化會將目前 Python runtime 加入唯讀例外，供診斷指令使用。其他工具鏈位於受限 home 內或非標準路徑時，在 init 加上精確的 `--runtime-read /absolute/toolchain/path`。這是所有角色共用的唯讀例外；不要指定整個 home、專案或 credential 目錄。`doctor --probe` 不通過時無法派工，請先檢查原因。建議使用一般 home 下的專案目錄；macOS 暫存目錄的特殊允許規則可能讓隔離檢查失敗。

在 Orch 對話中交辦，例如：

> 請 alpha worker 唯讀確認測試入口，beta worker 唯讀整理啟動步驟。各自附來源路徑及執行證據，禁止外部發送與修改。收齊後核對並回報。

尚無專案時，可先照 [兩個假專案示範](docs/demo.md) 試跑。

Orch 使用 `project_agents` MCP：`list_workers` → `send_message` → `check_worker_inbox` → `worker_status`／`fetch_inbox` → 核對保存 → `acknowledge_message`。送進信箱不等於開始執行；worker 回報也不等於人類驗收。

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
