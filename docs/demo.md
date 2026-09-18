# 用兩個假專案試跑

先安裝工具。以下資料夾名稱僅供示範；請選擇尚未使用的路徑：

```sh
mkdir -p ~/cpo-demo/alpha ~/cpo-demo/beta
printf 'alpha: synthetic example\n' > ~/cpo-demo/alpha/fixture.txt
printf 'beta: synthetic example\n' > ~/cpo-demo/beta/fixture.txt
cpo --state ~/.local/share/cpo-demo init \
  --workspace ~/cpo-demo/orch \
  --project alpha=~/cpo-demo/alpha \
  --project beta=~/cpo-demo/beta
cpo --state ~/.local/share/cpo-demo login
cpo --state ~/.local/share/cpo-demo start
cpo --state ~/.local/share/cpo-demo doctor --probe
cpo --state ~/.local/share/cpo-demo orch
```

交辦以下工作：

> 分別派 alpha、beta worker 唯讀讀取各自的 fixture.txt，使用不同 task_id。
> 禁止修改檔案與外部發送。請附實際命令及讀取結果，回覆後確認收件。
> Orch 收齊兩份回覆後核對 task_id，將結果保存至自己的 RESULT.md，再確認收件。

成功條件是兩個 worker 均回覆正確內容、原檔案不變、Orch 有保存並核對結果。
單純 send 成功或 worker 狀態 idle 都不足以證明成功。

完成後離開 Orch，再執行 `cpo --state ~/.local/share/cpo-demo stop`。
本工具不自動刪除示範資料或工作紀錄；需要清理時先確認 worker 已停止並保存結果。
