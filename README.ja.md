# launchd-why

[English](README.md)

Mac の launchd で予定どおりに動かなかったジョブについて、理由をコマンド1回で調べて報告する道具です。

Claude Code（`claude -p`）やスクリプトを `~/Library/LaunchAgents` から無人で動かしている人向けに作りましたが、ふつうのユーザーの LaunchAgent ならどれでも調べられます。plist と、そこから起動するスクリプトと、launchd の状態を読み、見つかった問題ごとに「何が起きているか」と「どう直すか」を1行ずつ出します。

- Python のファイル1つだけで、標準ライブラリしか使いません。入れる部品はありません
- 読むだけです。ジョブの読み込み・取り外し・起動・書き換えはしません
- 環境変数の値は表示も保存もしません。出すのは「ある／ない」だけです
- ログの行そのものは出しません。出すのは、エラーの種類と回数だけです

## 入れ方

macOS と Python 3.8 以上が要ります（Apple の Command Line Tools に入っている `python3` で動きます）。

```sh
curl -O https://raw.githubusercontent.com/vellum-office/launchd-why/main/launchd_why.py
chmod +x launchd_why.py
mkdir -p ~/bin && mv launchd_why.py ~/bin/launchd-why
```

`python3 launchd_why.py` でそのまま動かしてもかまいません。

## 使い方

```sh
# ~/Library/LaunchAgents のジョブを全部調べる（問題のないジョブは省きます）
launchd-why

# plist かスクリプトに claude が出てくるジョブだけ
launchd-why --only-claude

# 1つのジョブを、名前（Label）か plist のパスで
launchd-why com.example.nightly
launchd-why ~/Library/LaunchAgents/com.example.nightly.plist

# 成果のファイルが、最後の予定の時刻のあとに更新されたかも見る
launchd-why com.example.nightly --expect ~/reports/nightly.md

# 26 時間より古くないかを見る
launchd-why com.example.nightly --expect ~/reports/nightly.md --max-age 26

# スクリプトが必要とする環境変数（値は出しません）
launchd-why com.example.nightly --require-env MY_SERVICE_TOKEN

# 日本語か英語か（決めないときは LANG に合わせます）
launchd-why --lang ja
```

ほかのオプション：`--dir フォルダ`（別のフォルダの plist を調べる）、`--offline`（launchctl に聞かずファイルだけ見る）、`--env-file ファイル`（環境変数を渡しているファイルを足す）、`--grace 分`（予定の時刻から待つ分数。決めないときは 30）、`--stuck-minutes 分`（決めないときは 120）、`--json`、`--verbose`。

終了コードは、「問題」の項目が1つでもあれば 1、なければ 0 です。

### 出力の例

```
== com.example.nightly
   /Users/you/Library/LaunchAgents/com.example.nightly.plist
   [問題] ログの出し先（StandardOutPath）が保護されたフォルダ（Downloads）の中です: /Users/you/Downloads/nightly.log。ジョブはスクリプトを始める前に落ち、ログにも何も残りません（終了コード 78 のことが多い）。
          直し方: ログは ~/Library/Logs/ の下などに置いてください。
   [問題] claude が launchd の PATH では見つかりません（launchd の初期の PATH /usr/bin:/bin:/usr/sbin:/sbin）。ターミナルでは /opt/homebrew/bin/claude にあります。終了コード 127（command not found）になります。
          直し方: スクリプトの中で claude を絶対パスで呼ぶか、plist の EnvironmentVariables に PATH（/opt/homebrew/bin を含む）を足してください。
```

## 調べること

1. plist の書式と実行ファイル
   - `plutil -lint`、Label が無い、ProgramArguments が配列でなく文字列、書き間違いらしいキー（例：`StartCalenderInterval`）、StartCalendarInterval のおかしな値、動き出すきっかけが1つも無い、ファイルの中の `Disabled`、plist の持ち主と権限
   - 実行ファイルと、それが動かすスクリプトがあるか・実行できるか、相対パス、そのまま渡ってしまう `~` や `$HOME`（launchd はシェルを通さない）、引数の中の `>` や `|`、改行が CRLF、`#!` の行の実行ファイルが無い
2. 最後の終了の状態
   - `launchctl print gui/<uid>/<label>` を読みます。読み込まれていない、無効になっている、別のファイルから読み込まれている、直したのに読み込み直していない、最後の終了コードとその意味（1・2・78・126・127・シグナル・sysexits の値）、予定で動くジョブが長く動き続けている
3. 保護されたフォルダ
   - StandardOutPath や StandardErrorPath が `~/Desktop`・`~/Documents`・`~/Downloads`・iCloud Drive の中にある。launchd がそこにログを開けないため、スクリプトが始まる前に落ち、何も残りません（終了コード 78 が多い）。ログのフォルダが無い・書き込めない
   - スクリプト・作業フォルダ・読み込むファイルがそれらのフォルダにある場合は「危ない」として出します。動かす側にアクセスの許可があるときだけ動くためです
4. PATH
   - launchd は `PATH=/usr/bin:/bin:/usr/sbin:/sbin` でジョブを始めます。plist の EnvironmentVariables、ログインシェルの指定、スクリプトの中の `export PATH=` や `brew shellenv` から、ジョブが実際に受け取る PATH を組み立て、`claude`・`node`・`npx`・`python3`・`uv`・`jq`・`gh` などが見つかるかを調べます
   - launchd とターミナルで、別の `python3` を使うことになる場合は注意を出します
   - npm で入れた `claude` は `node` で動くので、ジョブの PATH に `node` が無いと落ちます。これも見つけます
   - PATH の値に `~` や `$HOME` が入っている（launchd は展開しない）
5. 環境変数
   - `claude` を呼ぶジョブでは、`CLAUDE_CODE_OAUTH_TOKEN` か `ANTHROPIC_API_KEY` を、plist・スクリプト・スクリプトが読み込むファイル・`--env-file` のファイル・`launchctl getenv` から探し、「ある・空・無い」を出します。値は出しません
   - その値を入れたファイルを他の人も読める権限なら、注意を出します
6. 時刻（指定したときだけ）
   - `--expect` を付けると、成果のファイルの更新時刻と、StartCalendarInterval・StartInterval で最後に動くはずだった時刻を比べます。終了コードが 0 なのにファイルが更新されていないときは、そう伝えます。終了コード 0 は、仕事が済んだ証拠にはなりません
7. ログの手がかり
   - ジョブのログの最後の 200 行から、"command not found"・"Operation not permitted"・"Permission denied"・"bad interpreter"・認証の失敗（トークンの期限切れ・ログインしていない）・利用の上限・Python の部品が見つからない、を探します

## 限界

- スリープと電源：予定の時刻に Mac がスリープしていると、StartCalendarInterval のジョブは起きたあとに1回だけ動きます。電源が切れていると、その回は飛ばされます。StartInterval は、逃した回を取り戻しません。その時刻に Mac が起きていたかはこの道具では分からず、`--expect` で分かるのは「成果が更新されていない」ことまでです
- launchd は、ジョブを最後に始めた時刻を残しません。ログと成果のファイルの更新時刻を手がかりにしています
- Claude Code が許可の確認画面で待っている状態は調べません。公開されていない内部のログに頼ることになり、更新で黙って壊れるためです
- 調べるのは、画面にログインしているユーザーの LaunchAgent（`gui/<uid>`）だけです。`/Library` の LaunchDaemons や、ほかのユーザーのジョブは見ません。SSH から動かすと launchctl が GUI の状態を見せないことがあります
- スクリプトの読み取りはできる範囲のものです。sh・bash・zsh・Python のファイルを読み、`source` を2段まで追い、単純な `export PATH=` を読みます。スクリプトを実行したり、条件分岐や関数の中身を追ったりはしません
- macOS のプライバシーの設定は読めないので、保護されたフォルダの中のスクリプトは「確実に落ちる」ではなく「危ない」として出します。最後の終了コードが 0 なら、参考に下げます
- 出力には、その Mac のファイルのパスが入ります。人に見せる場所へ貼る前に確かめてください
- macOS 26・Python 3.9 と 3.14 で試しました。`launchctl print` の表示の形は約束された仕様ではないので、macOS の版で変わることがあります

## 試し

```sh
python3 tests/run_tests.py
```

`tests/fixtures` の架空の plist を一時フォルダに写し、`--offline` で道具を動かします。`~/Library/LaunchAgents` にも launchd にも触りません。

## ライセンス

MIT です。[LICENSE](LICENSE) を見てください。
