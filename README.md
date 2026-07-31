# Strategy Research Lab v1

## 目的

これは「ツールを完成させるためのツール」ではありません。

**GPTと一緒に、崩れにくいEA手法を研究するための実験基盤**です。

- 上位足・下位足CSVを読み込む
- 指標とローソク足特徴を生成
- 条件を段階的に組み合わせて探索
- 学習・確認・未使用期間を分ける
- `strategy_ranking.csv` をGPTへ渡して次の仮説を立てる

## 拡張方法

EXEと同じフォルダに次を置きます。

```text
StrategyResearchLab.exe
plugins/
  indicators/
  signals/
presets/
```

### 設定値だけ変える

`presets/default.json` をコピーし、期間、RR、ATR倍率、最低取引数などを変更します。
EXE再作成は不要です。

### 新しい指標を追加する

`plugins/indicators/` に `.py` を追加します。
同梱の `rsi.py` が見本です。

### 新しいエントリー候補を追加する

`plugins/signals/` に `.py` を追加します。
同梱の `candlestick_extra.py` が見本です。

追加後、アプリの「プラグイン再読込」を押します。通常はEXE再作成不要です。

## 重要な限界

- pandas/numpyだけで書けるプラグインを前提とします。
- 新しい外部ライブラリが必要な機能、GUIそのものの変更、シミュレーション方式の根本変更、バグ修正はEXE更新が必要です。
- 「一度作れば永久に一切更新不要」は技術的に保証できません。
- 同一足でSLとTPの両方に到達した場合は保守的にSL扱いです。
- 研究結果は簡易シミュレーションです。採用候補は必ずMT5ストラテジーテスターで再検証してください。

## GitHubでEXEを作る

1. ZIPを展開してGitHubリポジトリへアップロード
2. Actions
3. Build Windows EXE
4. Run workflow
5. Artifactsからダウンロード

## 出力

- `strategy_ranking.csv`
- `features.csv`
- `settings_used.json`
- `research_report.txt`

## GPTとの研究サイクル

1. CSVで自動探索
2. `strategy_ranking.csv` と `research_report.txt` をGPTへ渡す
3. GPTが候補の弱点、偏り、追加条件を分析
4. preset変更、またはplugin追加
5. 再探索
6. 有望候補だけMT5 EA化


## settings_used.json の再読込

分析結果フォルダに出力された `settings_used.json` は、画面の
「設定JSONを選択・読込」から任意の場所を指定して読み込めます。

読み込み先は固定ではありません。ローカルPC上の任意のフォルダを選択できます。

### 読込エラー表示

GUIに以下を具体的に表示します。

- JSON構文エラーの行番号・列番号・該当行
- UTF-8以外の文字コード
- 必須項目の欠落
- 数値項目に文字が入っている
- 値の範囲が不正
- `train_end >= valid_end`
- `BUY` / `SELL` 以外の値
- 配列やオブジェクトの形式不正

ログ欄にも同じ内容を残します。
