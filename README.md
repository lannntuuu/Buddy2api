# Buddy2api 2.2

[English](README_EN.md) | 涓枃

> 鎶婃湰鏈哄凡缁忕櫥褰曠殑娑堣垂绾?AI 瀹㈡埛绔紝鎺ユ垚 OpenAI 鍏煎鎺ュ彛锛岀粰 Codex銆丱penCode銆丆herry Studio銆丯extChat 绛夌敤銆傞粯璁ゆ墦寮€ Work Buddy / CodeBuddy銆丵Claw銆佸崈闂姙鍏紙QwenWork锛夈€乀raeWork銆乀rae SOLO 浜斾釜閫氶亾锛汫MI 鏄?v2.2 鏂板鐨?opt-in 閫氶亾锛岄渶瑕佸湪 `CB_GATEWAY_PROVIDERS` 閲屽惎鐢ㄣ€傜鐞嗛〉涓嬫媺閫夊叾涓竴涓紝涓€娆¤姹傚彧璧颁竴涓€氶亾銆?

褰撳墠鐗堟湰 **2.2.0**銆傝繖涓」鐩彧閫傚悎鏈満鑷敤锛屼笉瑕佸叕寮€閮ㄧ讲锛屼篃涓嶈鎶婄櫥褰曞嚟鎹€丄PI Key銆佹暟鎹簱鏂囦欢鍙戠粰鍒汉銆倂2.2 閲嶇偣鍙樺寲锛氱鐞嗛〉涓嶅啀渚濊禆 CDN锛圴ue 涓?Sortable 鍏ㄩ儴鏈湴 vendor 鍖栵紝鏂綉涔熻兘鎵撳紑锛夛紱鍚庣涓変釜宸ㄧ煶妯″潡锛坄storage/database.py`銆乣gateway/server.py`銆乣upstream/proxy.py`锛夋寜鍩熸媶鍒嗭紱鏂板 GMI opt-in 閫氶亾銆傚畬鏁存洿鏂拌銆寁2.2 鏇存柊鍐呭銆嶃€?

## 杩欐槸浠€涔堬紵

Buddy2api 鍦ㄦ湰鏈烘彁渚?`http://127.0.0.1:8787/v1`銆備綘鍦ㄥ畼鏂瑰鎴风閲岀櫥褰曞苟涓旇繕鏈夐搴︼紝杩欎釜缃戝叧鎶婃湰鏈虹櫥褰曞鍏ヨ繘鏉ワ紝鎶婅姹傝浆鍒板搴斿巶鍟嗐€傛櫘閫氬鎴风璧?Chat Completions锛汣odex 璧?`/v1/responses`锛岀鐞嗛〉鎶?Key 绫诲瀷閫夋垚 Codex 鏃朵細鍋氫竴杞唴瀹规竻娲椼€?

浜斾釜閫氶亾榛樿閮藉紑锛岀鍏釜 GMI 榛樿鍏筹紙opt-in锛夈€傛病瑁呫€佹病鐧诲綍鐨勯€氶亾锛岃处鍙烽〉妫€娴嬩负绌猴紝涓嶄細鑷姩鍏ュ簱銆俆rae SOLO 涓嶈蛋鏈満鐧诲綍鐩綍锛岃蛋绠＄悊椤点€學eb 鐧诲綍銆嶆垨绮樿创鍥炶皟 URL锛堣涓嬶級銆?

```powershell
python -m src.gateway.server
```

| 閫氶亾 | 榛樿 | 鏈満鐧诲綍浣嶇疆 |
|---|---|---|
| WorkBuddy / CodeBuddy | 寮€ | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth` |
| QClaw | 寮€ | `%APPDATA%\QClaw` |
| 鍗冮棶鍔炲叕 QwenWork | 寮€ | `%APPDATA%\QwenWorkCN` |
| TraeWork | 寮€ | `%APPDATA%\TRAE SOLO CN\User\globalStorage` |
| Trae SOLO | 寮€ | 鏃狅紙Web 鐧诲綍闂幆 / 鍑瘉 JSON 瀵煎叆锛?|
| GMI | 鍏筹紙opt-in锛?| Web 閰嶇疆锛氳处鍙烽〉閫?GMI 閫氶亾鍚庣矘 API Key 鍗冲彲 |

璺緞涓嶅鏃跺彲鐢?`CB_AUTH_DIR`銆乣CB_QCLAW_AUTH_DIR`銆乣CB_QWENWORK_AUTH_DIR`銆乣CB_TRAEWORK_AUTH_DIR` 鎸囧畾銆傚洓涓€氶亾鐨勭櫥褰曟枃浠朵笉瑕佹贩鍦ㄥ悓涓€涓洰褰曘€俆rae SOLO 鐨勫嚟璇?JSON 鍙敤 `CB_TRAESOLO_AUTH_DIR` 鎸囧畾鎵弿鐩綍锛堝彲閫夛級銆侴MI 涓嶈鏈満鐧诲綍鐩綍锛岄潬绠＄悊椤靛鍏?API Key銆?

## 娉ㄦ剰浜嬮」

鎸変笅闈€屽畨瑁呬笌鍚姩銆嶅嵆鍙€傝繖鍑犳潯鏄?2.0 閲屾渶瀹规槗韪╃┖鐨勶細

1. **鍚姩鍚庤处鍙烽〉鏄┖鐨勶紝杩欐槸姝ｅ父鐨勩€?* 榛樿涓嶅啀鑷姩鍏ュ簱銆傚埌銆岃处鍙枫€嶉〉锛氶€夐€氶亾 鈫?閲嶆柊妫€娴?鈫?涓€閿鍏ャ€傚洓涓湰鍦伴€氶亾閮借兘閫夛紱**Trae SOLO 閫夊畬鍚庣偣銆屽彂璧风綉椤电櫥褰曘€?*锛屽湪鏂扮獥鍙ｅ畬鎴?TRAE 鐧诲綍锛屾祻瑙堝櫒浼氳嚜鍔ㄨ烦鍥炴湇鍔″畬鎴愬叆搴擄紙杩滅▼澶熶笉鍒板洖璋冩椂锛屾妸鍦板潃鏍忓畬鏁?URL 绮樿创鍒般€屾墜鍔ㄥ畬鎴愩€嶏級銆?
2. **涓€鎶?API Key 鍙墦涓€涓€氶亾銆?* 鍒涘缓鏃跺繀椤婚€夐€氶亾銆俉orkBuddy 鐨?Key 鍙?`auto` / `glm-5.2`锛決wenWork 鐨?Key 鍙?`auto` 鎴?`qwork-advanced`锛汿raeWork 鐨?Key 鍙?`auto` 鎴?`qwen-3.7-plus`锛汿rae SOLO 鐨?Key 鍙?`auto` 鎴?`glm-5.2`锛圫OLO 妯″瀷琛ㄨ緝澶э紝`/v1/models` 閲屼互 `traesolo/` 鍓嶇紑鍒楀嚭锛夈€傞€氶亾鍜屾ā鍨嬪涓嶄笂浼?400 鎴?403锛屼笉浼氬府浣犺浆鍒板彟涓€瀹躲€?
3. **鏌愪釜閫氶亾杩斿洖 503 `channel_unavailable`锛?* 杩欎釜閫氶亾杩樻病瀵煎叆鍙敤璐﹀彿銆?
4. **QClaw / QwenWork 璇峰湪 Windows 涓婄洿鎺ヨ窇 `python -m src.gateway.server`銆?* Linux Docker 璇讳笉浜嗚繖涓ゅ鐢?DPAPI 鍔犲瘑鐨勬湰鏈烘枃浠讹紱绠＄悊椤典細鍐欐槑杩欎竴鐐广€俉orkBuddy 鍙互缁х画鐢?Docker銆?
5. 鏈」鐩拰鑱婂ぉ瀹㈡埛绔渶濂藉湪鍚屼竴鍙扮數鑴戙€傚鎴风濡傛灉璺戝湪 Docker 閲岋紝Base URL 濉?`http://host.docker.internal:8787/v1`锛屼笉瑕佸～瀹瑰櫒鑷繁鐨?`127.0.0.1`銆?

## 瀹夎涓庡惎鍔?

杩樻病瑁呯幆澧冩椂鎸夎繖鍑犳璧般€傚凡缁忔湁铏氭嫙鐜鐨勶紝瑁呭畬 `ops/requirements/base.txt` 鍚庢墽琛?`python -m src.gateway.server` 鍗冲彲銆?

### 1. 瀹夎宸ュ叿

1. [Git](https://git-scm.com/downloads)锛學indows 淇濇寔榛樿閫夐」
2. [Miniconda](https://docs.conda.io/projects/miniconda/en/latest/)锛屾帹鑽?Python 3.12
3. 鍏堟墦寮€骞剁櫥褰曚綘瑕佺敤鐨勫畼鏂瑰鎴风锛堣嚦灏?Work Buddy / CodeBuddy锛?

瑁呭畬鍚?*閲嶆柊鎵撳紑** PowerShell銆乄indows Terminal 鎴?Anaconda Prompt锛?

```powershell
git --version
conda --version
```

鎵句笉鍒?`conda` 鏃讹紝鐢ㄥ紑濮嬭彍鍗曢噷鐨?**Anaconda Prompt / Miniconda Prompt**銆備篃鍙互鍦ㄩ偅閲屾墽琛?`conda init powershell`锛屽叧鎺夌獥鍙ｅ啀寮€銆?

### 2. 鍏嬮殕椤圭洰

```powershell
git clone https://github.com/wicm84266964/Buddy2api.git
cd Buddy2api
Get-ChildItem README.md, ops, gateway
```

鍚庨潰鐨勫懡浠ら兘瑕佸湪杩欎釜鐩綍閲屾墽琛屻€?

### 3. 鐢?Conda 鍚姩锛堟帹鑽愶級

```powershell
conda create -n buddy2api python=3.12 -y
conda activate buddy2api
python -m pip install --upgrade pip
python -m pip install -r ops/requirements/base.txt
python -m src.gateway.server
```

鐪嬪埌鐩戝惉淇℃伅鍚庯紝娴忚鍣ㄦ墦寮€锛?

```text
http://127.0.0.1:8787
```

鍋滄鏈嶅姟锛氬洖鍒扮粓绔寜 `Ctrl+C`銆備笅娆″紑鏈哄悗锛?

```powershell
cd <浣犵殑椤圭洰璺緞>\Buddy2api
conda activate buddy2api
python -m src.gateway.server
```

鎻愮ず绗﹀墠闈㈠簲鍑虹幇 `(buddy2api)`锛屽啀鎵ц `python -m pip`锛岄伩鍏嶈鍒扮郴缁?Python銆?

### 鍏朵粬鍚姩鏂瑰紡

- **鑴氭湰锛?* Windows 瀹夎 Python 鏃跺嬀閫?Add Python to PATH锛屽湪椤圭洰鐩綍鎵ц `.\ops\start.bat`銆侺inux / macOS锛歚chmod +x ops/start.sh && ./ops/start.sh`銆傝剼鏈紭鍏堢敤鍚嶄负 `buddy2api` 鐨?Conda 鐜锛屾病鏈?Conda 鎵嶅缓 `.venv`銆?
- **Docker锛?* `powershell -ExecutionPolicy Bypass -File .\ops\start-docker-win.ps1`銆傛湰鏈烘病鏈?WorkBuddy 鐧诲綍鐩綍鏃惰剼鏈粛浼氬惎鍔ㄣ€傚鍣ㄤ笅鎷夐噷浠嶆湁鍏釜閫氶亾锛堝紑 GMI 闇€ `CB_GATEWAY_PROVIDERS`锛夛紝浣?QClaw / QwenWork 璇风敤涓婇潰鐨?`python -m src.gateway.server`銆俆raeWork 鐧诲綍鏂囦欢涓嶆槸 DPAPI锛屾湰鏈?`python -m src.gateway.server` 瀵煎叆鍚?Docker 涔熻兘鐢ㄥ簱閲岀殑 token銆俆rae SOLO 涓嶈鏈満鐩綍锛岀櫥褰曢棴鐜笌 token 閮藉湪搴撻噷锛屽鍣ㄥ唴鍚屾牱鍙敤銆侴MI 璧?Web 瀵煎叆锛屽鍣ㄥ唴涔熺洿鎺ュ彲鐢ㄣ€?

### 绗竴娆℃墦寮€缃戦〉涔嬪悗

绠＄悊椤典笉鍐嶈嚜鍔ㄥ彂 Cookie銆傜涓€娆℃墦寮€缃戦〉鍚庯紝鍒般€岃缃€嶆妸鍚姩鏃ュ織閲岀殑 Admin Token 绮樿繘銆岀鐞嗛〉鐧诲綍銆嶄繚瀛樹竴娆★紝涔嬪悗娴忚鍣ㄥ嚟 HttpOnly Cookie 璁块棶銆?

1. 鎵撳紑銆岃处鍙枫€嶃€備笅鎷夐噷閫?WorkBuddy / QClaw / 鍗冮棶鍔炲叕 / TraeWork锛岀偣銆岄噸鏂版娴嬨€嶏紝鍐嶇偣銆屼竴閿鍏ユ湰鏈虹櫥褰曘€嶃€傞€?**Trae SOLO** 鏃舵敼鐢ㄣ€屽彂璧风綉椤电櫥褰曘€嶏細鏂扮獥鍙ｅ畬鎴?TRAE 鐧诲綍鍚庤嚜鍔ㄨ烦鍥炲叆搴擄紱杩滅▼澶熶笉鍒?`127.0.0.1` 鍥炶皟鏃讹紝鎶婃祻瑙堝櫒鍦板潃鏍忕殑瀹屾暣 URL 绮樿繘銆屾墜鍔ㄥ畬鎴愩€嶃€?
2. 鐐硅璐﹀彿鐨勩€屾祴璇曘€嶏紝鑳借繑鍥炰竴鍙ヨ瘽灏辫鏄庤繖鏉￠€氶亾閫氫簡銆?
3. 鎵撳紑銆孉PI Keys銆嶏紝**鍏堥€夊悓涓€涓€氶亾**鍐嶅垱寤恒€傜粰 Codex 鐢ㄦ椂 Key 绫诲瀷閫?Codex锛屾帴鍙ｇ敤 `/v1/responses`銆傚垱寤哄悗鍙互鍐嶆樉绀恒€佸鍒跺畬鏁?Key銆?
4. 鍦ㄥ鎴风閲屽～锛?
   - Base URL锛歚http://127.0.0.1:8787/v1`
   - API Key锛氬垰澶嶅埗鐨?Key
   - 妯″瀷锛歐orkBuddy 鐢?`auto` 鍗冲彲锛決Claw 鐢?`auto`锛涘崈闂姙鍏敤 `auto` 鎴?`qwork-advanced`锛汿raeWork 鐢?`auto` 鎴?`qwen-3.7-plus`锛汿rae SOLO 鐢?`auto` 鎴?`glm-5.2`锛坄auto` 鍦?SOLO 涓婅惤鍒?`glm-5.2`锛?

绠＄悊椤垫墦涓嶅紑鎴栬杩滅▼璁块棶鏃讹細

```powershell
$env:CB_GATEWAY_ADMIN_TOKEN="cb-admin-璇锋崲鎴愯冻澶熼暱鐨勯殢鏈哄€?
python -m src.gateway.server
```

### 鏇存柊

鍏?`Ctrl+C` 鍋滄帀姝ｅ湪璺戠殑鏈嶅姟锛?

```powershell
cd <浣犵殑椤圭洰璺緞>\Buddy2api
git pull --ff-only
conda activate buddy2api
python -m pip install -r ops/requirements/base.txt
python -m src.gateway.server
```

## 甯歌闂

- `git` 鎴?`conda` 涓嶆槸鍐呴儴鍛戒护锛氬叧鎺夌粓绔噸寮€锛汣onda 鐢ㄦ埛鏀圭敤 Miniconda Prompt銆?
- `No module named ...`锛氬厛 `conda activate buddy2api`锛屽啀 `python -m pip install -r ops/requirements/base.txt`銆?
- 涓嬭浇渚濊禆寰堟參锛氱‘璁よ兘璁块棶 PyPI锛屼笉瑕佹贩鐢ㄥソ鍑犱釜 Python銆?
- 绔彛 8787 琚崰鐢細鍏虫帀鏃х殑 Buddy2api锛屾垨 `python -m src.gateway.server --port 8788`銆?
- 缃戦〉閲屼竴涓处鍙烽兘娌℃湁锛氳繕娌″鍏ャ€傞€夊閫氶亾鍐嶆娴嬶紱鐧诲綍鐩綍涓嶅灏辫 `CB_AUTH_DIR` / `CB_QCLAW_AUTH_DIR` / `CB_QWENWORK_AUTH_DIR`銆?
- 鍒涘缓 Key 澶辫触锛氭病閫夐€氶亾銆?
- 瀹㈡埛绔?503 `channel_unavailable`锛氳繖涓?Key 缁戝畾鐨勯€氶亾杩樻病鏈夊彲鐢ㄨ处鍙枫€?
- 瀹㈡埛绔?403 `key_channel_mismatch`锛氭ā鍨嬪甫浜嗗埆鐨勯€氶亾鍓嶇紑锛屽拰褰撳墠 Key 涓嶄竴鑷淬€?
- 瀹㈡埛绔?400 `unknown_model`锛氭ā鍨嬩笉灞炰簬杩欐妸 Key 鐨勯€氶亾銆傛崲 Key锛屾垨鏀规垚璇ラ€氶亾璁よ瘑鐨?id銆?

## 浠?1.4.x 鍗囩骇

鍚姩鏃朵細鑷姩鏀规暟鎹簱銆傛棫 Key 瑙嗕负缁戝湪 `workbuddy` 涓婏紝鍘熸潵鐨?`auto` / `glm-5.2` 杩樿兘鐢ㄣ€?

鍜?1.4 涓嶅悓鐨勫湴鏂癸細鍚姩涓嶅啀鑷姩瀵煎叆璐﹀彿锛涚┖浠撴槸 503 鑰屼笉鏄櫘閫?`server_error`锛涙柊寤?Key 蹇呴』閫夐€氶亾锛涘畼鏂逛綑棰濆彧鏄剧ず绉垎锛屼笉鎶婂悇鍘傛暟瀛楀姞鍦ㄤ竴璧枫€?

## 瀹㈡埛绔帴鍏?

| 瀛楁 | 鍊?|
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | 绠＄悊椤靛垱寤猴紝宸茬粦瀹氶€氶亾 |
| 妯″瀷 | WorkBuddy锛歚auto` / `glm-5.2`銆俀Claw锛歚auto` 鎴?`qclaw/default`銆俀wenWork锛歚auto` 鎴?`qwork-advanced`銆俆raeWork锛歚auto` 鎴?`qwen-3.7-plus`銆俆rae SOLO锛歚auto` / `glm-5.2` / `traesolo/...`锛堝畬鏁村垪琛ㄨ `/v1/models`锛?|
| Stream | 寤鸿寮€ |

鎺ュ彛锛歚/v1/chat/completions`銆乣/v1/responses`銆乣/v1/models`銆傛病鍔犲墠缂€鐨?`auto` 璧拌繖鎶?Key 缁戝畾鐨勯€氶亾銆侰odex 鐢?Responses 鎺ュ彛锛涚鐞嗛〉閫?Codex 绫诲瀷鐨?Key 浼氭寜 Codex 鐗瑰緛 prompt 鍋氭竻娲楋紙鍏跺畠瀹㈡埛绔€熺敤杩欐妸 Key銆佷絾娌℃湁 Codex 鐗瑰緛鏃朵笉鏀瑰啓锛夈€?

OpenCode 绀轰緥锛圵orkBuddy Key锛夛細

```json
{
  "provider": {
    "workbuddy": {
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "http://127.0.0.1:8787/v1",
        "apiKey": "sk-cb-浣犵殑key"
      },
      "models": {
        "auto": { "name": "WorkBuddy Auto" },
        "glm-5.2": { "name": "GLM-5.2" }
      }
    }
  }
}
```

```powershell
opencode run -m workbuddy/auto "浣犲ソ"
```

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-cb-浣犵殑key" \
  -d '{"model":"auto","messages":[{"role":"user","content":"浣犲ソ"}]}'
```

QwenWork銆丵Claw銆乀raeWork銆乀rae SOLO 鍚勭敤鑷繁閭ｆ妸 Key锛屼笉瑕佹贩鐢ㄣ€傛敞鎰?`glm-5.2` 鍦?WorkBuddy 鍜?Trae SOLO 涓や釜閫氶亾閮藉瓨鍦細涓嶅甫鍓嶇紑鏃舵寜 Key 缁戝畾鐨勯€氶亾瑙ｆ瀽锛屾兂鏄庣‘鎸?SOLO 灏辩敤 `traesolo/glm-5.2`銆?

### 鎸夐€氶亾閰嶇疆妯″瀷鍒楄〃

鍚勯€氶亾鐨勬ā鍨嬪垪琛?鍒悕鍙€氳繃绠＄悊 API 閰嶇疆锛堟敼瀹岀珛鍗崇敓鏁堬紝鏃犻渶閲嶅惎锛夛紱涓嶉厤缃椂鐢ㄥ唴缃粯璁ゃ€?

```bash
# 鏌ョ湅锛堝惈鐢熸晥鍊笺€佸唴缃粯璁ゃ€佹槸鍚﹁嚜瀹氫箟锛?
curl -H "Authorization: Bearer <admin-token>" http://127.0.0.1:8787/admin/channels/traework/models

# 淇敼锛坢odels 鏁翠綋鏇挎崲锛沶ull 閲嶇疆涓洪粯璁わ級
curl -X PUT -H "Authorization: Bearer <admin-token>" -H "Content-Type: application/json" \
  http://127.0.0.1:8787/admin/channels/traework/models \
  -d '{"models":["qwen-3.7-plus","glm-5"],"aliases":{"auto":"qwen-3.7-plus"}}'
```

瑙勫垯锛歚models` 涓洪潪绌哄瓧绗︿覆鏁扮粍锛堟垨 `{"id": "..."}` 瀵硅薄锛夛紝`aliases` 涓?`鍒悕 -> 妯″瀷id` 鐨勯潪绌哄璞★紱
涓€娆¤姹傝嚦灏戜紶涓€椤广€傝嚜瀹氫箟鍒楄〃鏄櫧鍚嶅崟锛屼笉鍦ㄥ垪琛ㄥ唴鐨勬ā鍨嬪璇ラ€氶亾 400锛圦Claw 鐨?`pool-*` 鍓嶇紑闄ゅ锛夈€?
WorkBuddy 鍏煎鍘嗗彶璁剧疆閿?`models` / `model_aliases`锛涘叾瀹冮€氶亾瀛?`<channel>.models` / `<channel>.aliases`銆?

### 缁熶竴妯″瀷锛堣法骞冲彴缈昏瘧灞傦級

鍚屼竴涓ā鍨嬪湪涓嶅悓骞冲彴鍚嶅瓧涓嶄竴鏍锋椂锛屽畾涔変竴娆＄粺涓€妯″瀷锛堢粺涓€鍚嶄互 WorkBuddy 鍛藉悕涓哄噯锛夛紝
瀹㈡埛绔彧璇锋眰缁熶竴鍚嶏紝缃戝叧鎸?Key 缁戝畾骞冲彴缈昏瘧鎴愯骞冲彴鍐呴儴鍚嶏紱涔嬪悗鐓ф棫璧扮櫧鍚嶅崟鏍￠獙
锛堝唴閮ㄥ悕涓嶅湪鐧藉悕鍗曚粛 400锛岀粺涓€妯″瀷涓嶈嚜鍔ㄨ繘鐧藉悕鍗曪級銆?

```bash
curl -X PUT -H "Authorization: Bearer <admin-token>" -H "Content-Type: application/json" \
  http://127.0.0.1:8787/admin/unified-models \
  -d '{"models":[{"name":"deepseek-v4-flash","mappings":{"traework":"DeepSeek-V4-Flash-Official","workbuddy":"deepseek-v4-flash"}}]}'
```

缃戦〉绠＄悊椤点€屾ā鍨嬮厤缃€嶉〉鎻愪緵鍥惧舰鐣岄潰锛氥€岀粺涓€妯″瀷銆嶅琛紙涓€琛屼竴涓粺涓€妯″瀷銆佹瘡鍒椾竴涓钩鍙帮紝
鏍煎瓙濉唴閮ㄥ悕銆佺暀绌?= 璇ュ钩鍙版病鏈夛級+銆屽悇骞冲彴璁剧疆銆嶅彲鍒囨崲鍒楄〃锛堟瘡骞冲彴鐨勭櫧鍚嶅崟涓庡埆鍚嶏級銆?

## 鍚姩鍙傛暟

| 鍙傛暟 | 榛樿 | 璇存槑 |
|---|---|---|
| `--host` | `127.0.0.1` | 鐩戝惉鍦板潃锛屾湰鏈虹敤淇濇寔杩欎釜鍊?|
| `--port` | `8787` | 绔彛 |
| `--admin-token` | 鑷姩鐢熸垚锛堝惎鍔ㄦ棩蹇楁墦鍗颁竴娆★級 | 绠＄悊 Token锛涘湪绠＄悊椤点€岃缃€嶇矘璐翠竴娆″嵆鍙嬁鍒?Cookie |
| `--no-admin-auth` | 鍏?| 鍏虫帀绠＄悊閴存潈锛屽彧閫傚悎鏈満涓存椂璇?|
| `--config` | 璇?`config.toml` 鐨?`[default]` 鍧楋紱甯﹁矾寰勫垯褰撲綔 TOML 鏂囦欢璺緞锛涗笉甯﹁矾寰勫垯褰撲綔 profile 鍚嶏紙鍙傝 [閰嶇疆鏂囦欢](#閰嶇疆鏂囦欢)锛?|
| `--config-name` | `default` | TOML 鏂囦欢鍐呰鍔犺浇鐨?profile 琛ㄥ悕锛坄[dev]` / `[prod]` 绛夛級 |

## 閰嶇疆鏂囦欢

`config.toml` 鏀惧湪椤圭洰鏍圭洰褰曪紝鍚姩鏃惰 `gateway.server` 鑷姩鍔犺浇銆傞€傚悎"鎴戜笉鎯虫瘡娆¤涓€鍫?CLI 鍙傛暟 / 鐜鍙橀噺"鐨勫満鏅紝鎶?`host` `port` `database.path` `admin.token` 鍐欒繘鏂囦欢锛宐are `python -m src.gateway.server` 灏辫兘鐩存帴鐢ㄣ€?

**浼樺厛绾?*锛坙ater wins锛夛細

```
浠ｇ爜榛樿鍊? 鈫? config.toml [default]  鈫? config.toml [<profile>]  鈫? 鐜鍙橀噺  鈫? CLI 鍙傛暟
```

**涓ょ profile 鍔犺浇鏂瑰紡**锛?

```bash
# 1) profile 鍐欏湪鍚屼竴涓?config.toml 閲?
python -m src.gateway.server                          # 鐢?[default] 鍧?
python -m src.gateway.server --config prod            # 鐢?[prod] 鍧?
CB_GATEWAY_CONFIG=prod python -m src.gateway.server   # 鍚屼笂锛屼絾閫氳繃鐜鍙橀噺

# 2) profile 鍦ㄧ嫭绔嬫枃浠堕噷
python -m src.gateway.server --config config.prod.toml
```

**瀹屾暣绀轰緥**锛坉ev / prod 鍏变韩鍚屼竴浠戒唬鐮併€佸悇鑷竴浠介厤缃級锛?

```toml
# config.toml 路 dev checkout 榛樿鐢?8787
[default.server]
host = "127.0.0.1"
port = 8787

[dev.server]
host = "127.0.0.1"
port = 8787
```

```toml
# config.prod.toml 路 prod checkout 璧?8788銆佽嚜宸辩殑 data 鐩綍
[default.server]
host = "127.0.0.1"
port = 8788

[default.database]
path = "/var/lib/buddy2api/codebuddy_gateway.db"

[default.admin]
# 鐣欑┖灏辫嚜鍔ㄧ敓鎴愶紱濉簡灏卞浐瀹氫笅鏉ワ紙娴忚鍣?Cookie 璺ㄩ噸鍚湁鏁堬級
# token = "cb-admin-xxxxxxxxxxxxxxxxxxxxxxxx"
```

`config.toml` 鍜?`config.*.toml` 閮借 `.gitignore` 鎺掗櫎锛坧er-deploy 閰嶇疆涓嶈繘 git锛夛紝璺熻釜鐨勫彧鏈?`config.example.toml` 妯℃澘銆?

**鍚屾椂璺?dev + prod**锛氫袱涓?checkout 鍚勫啓涓€浠?`config.toml`锛岀鍙ｅ拰 db 璺緞蹇呴』閿欏紑锛堝惁鍒?WAL 閿佷細鍐茬獊锛夛細

| checkout | config.toml 绔彛 | config.toml db 璺緞 |
|---|---|---|
| `Buddy2api/`锛坉ev锛?| 8787 | `data/codebuddy_gateway.db`锛堥粯璁わ級 |
| `Buddy2api-prod/`锛坵orktree 璺戝疄渚嬶級 | 8788 | `data/codebuddy_gateway.db`锛堢浉瀵?prod 鑷繁鐨?cwd锛?|

鍥哄畾 admin token锛氱紪杈?`config.toml` 鐨?`admin.token = "cb-admin-xxx"`銆傜敓鎴愪竴涓細`python -c "import secrets; print('cb-admin-' + secrets.token_urlsafe(24))"`銆?

## 鐜鍙橀噺

> 鍏ㄩ儴鍙€夛紝閮芥湁鍚堢悊鐨勯粯璁ゅ€硷紱缁濆ぇ澶氭暟鍦哄悎**浠€涔堥兘涓嶇敤璁?*銆傚彉閲忔寜鐢ㄩ€斿垎缁勶紝鍗曡鏄庨噷鎷彿鍐呬负璇ュ彉閲忕殑榛樿鍊硷紝`*` 琛ㄧず鍙湪鐗规畩鍦哄悎鐢ㄣ€?

### 鏍稿績 / 鍚姩
| 鍙橀噺 | 璇存槑 |
|---|---|
| `CB_GATEWAY_PROVIDERS` | 鍚敤鍝簺閫氶亾锛岄€楀彿鍒嗛殧銆傞粯璁?`workbuddy,qclaw,qwenwork,traework,traesolo`銆侴MI 涓嶅湪榛樿閲岋紝鍚敤鍔犲湪鏈熬锛歚workbuddy,qclaw,qwenwork,traework,traesolo,gmi` |
| `CB_GATEWAY_AUTO_IMPORT` | 璁?`1` 鍒欏惎鍔ㄦ椂鑷姩鎵弿瀵煎叆璐﹀彿銆傞粯璁?`0` |
| `CB_GATEWAY_CHECKIN_GAP_MS` | 涓€閿鍙栨椂鐩搁偦璐﹀彿鐨勯棿闅旀绉掞紙闃查鎺э紝涓嶅彲璁惧お灏忥級銆傞粯璁?`800` |
| `CB_GATEWAY_ADMIN_TOKEN` | 鍥哄畾绠＄悊 Token銆傞粯璁よ嚜鍔ㄧ敓鎴愶紙鍚姩鏃ュ織鎵撳嵃涓€娆★紝绠＄悊椤点€岃缃€嶇矘璐翠竴娆″嵆鍙嬁 Cookie锛?|
| `CB_GATEWAY_DB_PATH` | 鏁版嵁搴撴枃浠惰矾寰勩€傞粯璁ら」鐩笅 `data/` 鍐?|
| `CB_GATEWAY_MASTER_KEY` | 璺ㄧ郴缁熸惉鏁版嵁搴撴椂鎵嬪姩鎸囧畾鐨勫姞瀵嗕富瀵嗛挜銆傞粯璁ゆ瘡瀹炰緥鑷姩鐢熸垚锛堟崲鏈哄櫒鎴栧垹 data 浼氬け鏁堬紝闇€杩佺Щ鏃剁敤锛?|
| `CB_GATEWAY_CREDENTIAL_KEY_FILE` * | 璇诲彇鍔犲瘑涓诲瘑閽ョ殑鏂囦欢璺緞锛圖ocker 鍦烘櫙娉ㄥ叆鐢級銆傞粯璁ょ┖锛屽嵆鐢?`CB_GATEWAY_MASTER_KEY` 鎴栬嚜鍔ㄧ敓鎴?|
| `CB_GATEWAY_SECURE_COOKIE` | 璁?`1` 寮哄埗绠＄悊 Cookie 璧?Secure锛坔ttps 鎴栧弽鍚戜唬鐞嗗悗锛夈€傞粯璁よ窡闅忚姹傚崗璁?|
| `CB_GATEWAY_LOG_RETENTION_DAYS` | 鏃ュ織淇濈暀澶╂暟銆傞粯璁?`90` |

### 鍚勯€氶亾鐧诲綍鐩綍
| 閫氶亾 | 鍙橀噺 | 璇存槑 |
|---|---|---|
| WorkBuddy | `CB_AUTH_DIR` | 鏈満鐧诲綍鐩綍 |
| QClaw | `CB_QCLAW_AUTH_DIR` | 鏈満鐧诲綍鐩綍 |
| QwenWork | `CB_QWENWORK_AUTH_DIR` | 鏈満鐧诲綍鐩綍 |
| TraeWork | `CB_TRAEWORK_AUTH_DIR` | `storage.json` 鎵€鍦ㄧ洰褰?|
| Trae SOLO | `CB_TRAESOLO_CALLBACK_BASE` | 鐧诲綍鍥炶皟鍩哄湴鍧€锛堣繙绋嬮儴缃叉椂鎸囧悜鑳戒粠澶栫綉璁块棶鏈嶅姟鐨勫湴鍧€锛岄粯璁ょ敤璇锋眰鑷韩鍦板潃锛?|
| Trae SOLO | `CB_TRAESOLO_AUTH_DIR` * | 鍑瘉 JSON 鎵弿鐩綍锛堝彲閫夛紱璇ラ€氶亾榛樿涓嶆壂鐩綍锛岃蛋 Web 鐧诲綍锛?|

> `CB_HOST_AUTH_DIR` 浠?Docker 閮ㄧ讲鑴氭湰鍐呴儴浣跨敤锛堟寕杞界殑鏈満 WorkBuddy 鐩綍锛夛紝`CB_CONTAINER_AUTH_DIR` 鏄鍣ㄥ唴鐨勬寕杞界偣锛堥粯璁?`/auth`锛夛紝涓€鑸笉鐢ㄧ銆?

### WorkBuddy 鍑虹珯鎸囩汗锛圲ser-Agent / 鐗堟湰澶达級
| 鍙橀噺 | 璇存槑 |
|---|---|
| `CB_GATEWAY_USER_AGENT` | 鏁翠綋瑕嗙洊鏁翠釜 User-Agent銆傞粯璁?`CLI/2.109.2 CodeBuddy/2.109.2`锛岃 `codebuddy2openai/2.0` 鍙洖閫€鍘嗗彶 UA銆傚彧褰卞搷 WorkBuddy 鍑虹珯 |
| `CB_GATEWAY_IDE_VERSION` | CLI 鐗堟湰鍙凤紝椹卞姩 UA 涓?X-IDE-Version銆傞粯璁?`2.109.2` |
| `CB_GATEWAY_STAINLESS_OS` * | 涓婃姤鐨勬搷浣滅郴缁熷瓧绗︿覆銆傞粯璁ゆ寜褰撳墠骞冲彴鎺ㄦ柇 |
| `CB_GATEWAY_STAINLESS_PACKAGE_VERSION` * | `stainless` 鍖呯増鏈€傞粯璁?`5.10.1` |
| `CB_GATEWAY_NODE_VERSION` * | Node 杩愯鏃剁増鏈€傞粯璁?`v22.13.1` |

### 璇锋眰 / 椋庨櫓鎺у埗
| 鍙橀噺 | 璇存槑 |
|---|---|
| `CB_GATEWAY_CORS_ORIGINS` | 鍏佽鐨?CORS 鏉ユ簮锛岄€楀彿鍒嗛殧銆傞粯璁?`http://127.0.0.1:8787,http://localhost:8787` |
| `CB_GATEWAY_ALLOW_UNAUTHENTICATED_API` | 璁?`1` 鍏佽鏃?API key 璇锋眰锛堝彧閫傚悎鏈満涓存椂娴嬶級銆傞粯璁?`0` |
| `CB_GATEWAY_MAX_BODY_BYTES` | 璇锋眰浣撲笂闄愬瓧鑺傘€傞粯璁?`10MiB` |
| `CB_GATEWAY_USAGE_RATE_LIMIT` | /usage 鎺ュ彛绉掔骇闄愭祦锛岃 `0` 鍏抽棴銆傞粯璁?`30` |
| `CB_GATEWAY_TOOL_STALL_RETRY` | 宸ュ叿鍋滆浆鏃惰嚜鍔ㄧ敤 `tool_choice=required` 閲嶈瘯涓€娆°€傞粯璁?`1` |
| `CB_GATEWAY_TOOL_STALL_FAIL_STREAM` * | 娴佸紡宸ュ叿鍋滆浆涓旈噸璇曚篃澶辫触鏃讹紝鎶婂洖鍚堟爣璁颁负澶辫触鑰屼笉鏄繑鍥炴鏂囥€傞粯璁?`0` |

### 鎬濊€冩。浣嶏紙鎸夋ā鍨嬶級

涓嶅啀鐢ㄧ幆澧冨彉閲忥紝鏀逛负鍦ㄧ鐞嗛〉銆岄€氶亾涓庢ā鍨?鈫?鍚勫钩鍙拌缃€嶉噷**鎸夋ā鍨?*閰嶇疆锛堝瓨鏁版嵁搴擄紝鍗虫椂鐢熸晥锛夛細

- 姣忎釜妯″瀷涓€涓笅鎷夛細`榛樿锛堜笉娉ㄥ叆锛塦 / `none` / `minimal` / `low` / `medium` / `high` / `max`锛涘彟鏈夈€岄€氶亾榛樿銆嶆。浣嶄綔鐢ㄤ簬鏈崟鐙缃殑妯″瀷銆?
- 浼樺厛绾э細瀹㈡埛绔樉寮?`reasoning_effort` > 鎸夋ā鍨嬮厤缃?> 閫氶亾榛樿 > 涓嶆敞鍏ワ紙璺熼殢涓婃父榛樿锛夈€?
- 浠?WorkBuddy 閫氶亾涓婃父锛坄copilot.tencent.com`锛夌‘璁ゆ敮鎸佽鍙傛暟锛涘叾瀹冮€氶亾鍦?UI 鏄剧ず銆屼笉鏀寔銆嶃€?
- 瀹炴祴鍘熺敓鎺ュ彈鍊艰 `docs/design/per-model-reasoning-effort.md`銆傛敞鎰忥細deepseek/glm/auto 榛樿涓嶆€濊€冦€侀€夋。浣?寮€鍚€濊€冿紙浼氬彉鎱級锛涙兂鏈€蹇彲缁?DeepSeek 閫?`low` 鎴栫暀绌恒€俙off` 涓婃父涓嶆帴鍙楋紙11150锛夈€?

### content 绮剧畝锛坵orkbuddy 11128 鎷︽埅鑷剤锛?
| 鍙橀噺 | 璇存槑 |
|---|---|
| `CB_GATEWAY_COMPACT_CHARS` | 鎵嬪姩鍏ㄥ眬寮€鍚簿绠€瓒呭ぇ璇锋眰浣擄紝骞舵寚瀹氬崟瀛楁瀛楃闃堝€笺€傞粯璁?`0`锛堝叧闂紝璧颁笅鏂圭殑鎸夐€氶亾鑷剤锛?|
| `CB_GATEWAY_COMPACT_ARMED_CHARS` * | 鏌愰€氶亾鐪熻Е鍙戣繃涓€娆?11128 鍚庯紝璇ラ€氶亾鑷姩绮剧畝鐨勫崟瀛楁闃堝€笺€傞粯璁?`3000` |
| `CB_GATEWAY_COMPACT_SYSTEM_CHARS` * | system 娑堟伅闃堝€硷紙绾ご閮ㄦ埅鏂紝瀹炴祴鍏跺熬閮?git/commit 鍧楁槸 11128 瑙﹀彂婧愶級銆傞粯璁?`5000` |

> 璇﹁ `docs/workbuddy-11128-troubleshoot.md`锛氭甯歌姹傞粯璁や笉鎴柇锛屾煇閫氶亾杩斿洖 11128 鍚庤嚜鍔ㄦ瑁呭苟绮剧畝锛坰ystem 绾ご鍒?5000銆佽秴澶?content/reasoning 澶村垏銆乼ools 鎻忚堪绮剧畝锛岀粨鏋勯敭涓?`tool_calls` 姘镐笉鎴級锛宍/admin/stats` 鐨?`compaction` 瀛楁鍙湅鐢熸晥鎯呭喌銆?

### 璋冭瘯
| 鍙橀噺 | 璇存槑 |
|---|---|
| `CB_DEBUG_DUMP` * | 鎶?responses 鍗忚鐨勮姹?鍝嶅簲锛堣劚鏁?JSON锛塪ump 鍒?`upstream/.debug/` 渚夸簬鎺掓煡鍑虹珯鍗忚銆傞粯璁ゅ叧 |
| `CB_DEBUG_DUMP_INCLUDE_CONTENT` * | dump 鏃惰繛 content 涓€璧峰啓锛堥粯璁よ劚鏁忎笉鍚鏂囷級銆傞粯璁ゅ叧锛屼粎闅?`CB_DEBUG_DUMP` 涓€璧风敤 |
| `CB_DOCKER` * | 鏍囪杩愯鍦?Docker 鍐咃紙鍐呴儴鍒ゆ柇鐢級銆傞粯璁ょ┖ |

## Credit 涓?Token 缁熻

鍚勯€氶亾鐨?token / credit 缁熻琛屼负涓嶄竴鑷达細

- **WorkBuddy** token 涓?credit 閮界敱涓婃父鐩存帴鎶ワ紱
- **Trae SOLO / QClaw / QwenWork** token 鐢变笂娓告姤銆乧redit 涓嶆姤锛?
- **TraeWork** token 涓?credit 閮戒笉鎶ワ紙SSE 閲?`token_usage` 浜嬩欢琚涪锛夈€?

浠?v2.2.0 璧凤紝traesolo/qclaw/qwenwork 涓夊鍙惎鐢?*缃戝叧渚?token鈫抍redit 浼扮畻**锛堟瘡閫氶亾鍦?
銆屾ā鍨嬮厤缃?鈫?鍚勫钩鍙拌缃€嶉噷璁?`credit_rate`锛岄粯璁?1000 token / 1 credit锛夈€傝繖鏄?*浼扮畻鍊间笉鏄湡瀹炴墸璐?*锛?
鍙敤浜庣湅瓒嬪娍鍜屽仛鍐呴儴浼扮畻锛屼笉瑕佹嬁瀹冨拰涓婃父鐪熷疄浣欓鍋氬樊棰濆璐︺€?
TraeWork 鎯崇畻闇€瑕佸厛鍗曠嫭淇畠鐨?SSE 瑙ｆ瀽锛屾湭鍋氥€傝瑙?`docs/credit-and-token-tracking.md`銆?

## 鏁版嵁鍜屽畨鍏?

- 璐﹀彿 Token 鍐欏叆鍓嶄細鍔犲瘑銆俉indows 鐢ㄧ郴缁?DPAPI銆?
- 涓嶈鎶?`*.db`銆佺櫥褰曠洰褰曘€佹棩蹇椼€佸甫 Key 鐨勬埅鍥惧彂鍑哄幓銆?
- 涓嶈鎶婃湇鍔＄粦鍒板叕缃戙€備繚鎸?`127.0.0.1`銆?

## 椤圭洰缁撴瀯

v2.2 鎶婁笁涓法鐭虫ā鍧楁寜鍩熸媶寮€锛泇2.3 鎶?6 涓簮妯″潡缁熶竴杩?`src/`銆乣redesign-audit/` 杩?`docs/redesign/`銆傜洰褰曞竷灞€濡備笅锛?

```text
Buddy2api/
鈹溾攢鈹€ src/                    # 鍏ㄩ儴 Python 涓庡墠绔簮
鈹?  鈹溾攢鈹€ gateway/            # HTTP 鍏ュ彛锛團astAPI 搴旂敤 + 璺敱 + 鐗堟湰鍙凤級
鈹?  鈹?  鈹溾攢鈹€ server.py       # app 宸ュ巶銆乴ifespan銆丼taticFiles 鎸傝浇
鈹?  鈹?  鈹溾攢鈹€ router.py       # 缁戝畾璇锋眰鍒伴€氶亾銆佸仛妯″瀷缈昏瘧锛堝伐鍏凤級
鈹?  鈹?  鈹溾攢鈹€ deps.py         # 鍏变韩閴存潈渚濊禆
鈹?  鈹?  鈹溾攢鈹€ routers/
鈹?  鈹?  鈹?  鈹溾攢鈹€ admin.py        # /admin/* 绔偣
鈹?  鈹?  鈹?  鈹溾攢鈹€ v1.py           # /v1/chat/completions銆?v1/responses銆?v1/models
鈹?  鈹?  鈹?  鈹斺攢鈹€ static_router.py# /admin/meta 绛夊厓淇℃伅
鈹?  鈹?  鈹斺攢鈹€ version.py
鈹?  鈹溾攢鈹€ accounts/           # 璐﹀彿涓庨€氶亾绠＄悊
鈹?  鈹?  鈹溾攢鈹€ auth_manager.py     # 璐﹀彿閫夋嫨銆乼oken 绠＄悊銆乧heckin
鈹?  鈹?  鈹斺攢鈹€ control_plane.py    # 鍚姩鎵弿銆佷竴閿鍙栥€佹ā鍨嬮厤缃?
鈹?  鈹溾攢鈹€ upstream/           # 涓婃父瀵规帴
鈹?  鈹?  鈹溾攢鈹€ proxy.py        # pipeline 涓绘祦绋嬶紙proxy_chat_completions 绛夛級
鈹?  鈹?  鈹溾攢鈹€ aliases.py      # 妯″瀷鍒悕琛ㄣ€侀粯璁ゆā鍨嬨€佹€濊€冩。浣?
鈹?  鈹?  鈹溾攢鈹€ moderation.py   # 鍐呭瀹℃牳銆佸伐鍏峰仠杞娴?
鈹?  鈹?  鈹溾攢鈹€ compaction.py   # 璇锋眰浣撶簿绠€銆?1128 鑷剤
鈹?  鈹?  鈹斺攢鈹€ responses.py    # OpenAI Responses 鈫?Chat Completions 缈昏瘧
鈹?  鈹溾攢鈹€ storage/            # 鍩虹璁炬柦灞傦紙DB銆佸姞瀵嗐€佹寚绾广€佺紦瀛橈級
鈹?  鈹?  鈹溾攢鈹€ database.py     # 鍏煎闂ㄩ潰锛坮e-export 鑷?storage.repos锛?
鈹?  鈹?  鈹溾攢鈹€ backup.py       # db 蹇収 / rotation / 鍑嵁鍚屾
鈹?  鈹?  鈹溾攢鈹€ repos/
鈹?  鈹?  鈹?  鈹溾攢鈹€ accounts.py     # 璐﹀彿 CRUD
鈹?  鈹?  鈹?  鈹溾攢鈹€ api_keys.py     # API Key CRUD
鈹?  鈹?  鈹?  鈹溾攢鈹€ logs.py         # 璇锋眰鏃ュ織銆佹煡璇?
鈹?  鈹?  鈹?  鈹溾攢鈹€ settings.py     # 閫氶亾閰嶇疆銆並V
鈹?  鈹?  鈹?  鈹溾攢鈹€ stats.py        # dashboard 鑱氬悎
鈹?  鈹?  鈹?  鈹斺攢鈹€ _common.py      # 鍏变韩杩炴帴 / Schema
鈹?  鈹?  鈹溾攢鈹€ credit_cache.py     # 鍚勯€氶亾 credit 缂撳瓨
鈹?  鈹?  鈹溾攢鈹€ http_pool.py        # 涓婃父 httpx 瀹㈡埛绔睜
鈹?  鈹?  鈹溾攢鈹€ credential_crypto.py
鈹?  鈹?  鈹斺攢鈹€ fingerprint.py
鈹?  鈹溾攢鈹€ providers/          # 閫氶亾閫傞厤
鈹?  鈹?  鈹溾攢鈹€ workbuddy/
鈹?  鈹?  鈹溾攢鈹€ qclaw/
鈹?  鈹?  鈹溾攢鈹€ qwenwork/
鈹?  鈹?  鈹溾攢鈹€ traework/
鈹?  鈹?  鈹溾攢鈹€ traesolo/
鈹?  鈹?  鈹斺攢鈹€ gmi/            # v2.2 鏂板锛宱pt-in
鈹?  鈹斺攢鈹€ web/                # 绠＄悊椤?UI
鈹?      鈹溾攢鈹€ index.html
鈹?      鈹溾攢鈹€ css/app.css
鈹?      鈹溾攢鈹€ js/
鈹?      鈹?  鈹溾攢鈹€ app.js      # 鍏ュ彛
鈹?      鈹?  鈹溾攢鈹€ api.js      # 鍚庡彴 API 瀹㈡埛绔?
鈹?      鈹?  鈹溾攢鈹€ icons.js    # 鑷粯 SVG 鍥炬爣
鈹?      鈹?  鈹斺攢鈹€ pages/      # dashboard / accounts / quota / keys / channels / usage / logs / setup / settings
鈹?      鈹斺攢鈹€ vendor/         # Vue 3.4.21 + SortableJS 1.15.6锛堟湰鍦帮紝鏂綉鍙敤锛?
鈹溾攢鈹€ docs/                   # 璁捐涓庝娇鐢ㄦ枃妗?
鈹?  鈹溾攢鈹€ *.md                # credit-and-token-tracking / dashboard-slow-query / provider-model-usage / traesolo-usage / traework-usage / workbuddy-11128 / cache-tracking
鈹?  鈹溾攢鈹€ design/             # per-model-reasoning-effort 绛夎璁＄
鈹?  鈹溾攢鈹€ maintenance/        # 缁存姢鎵嬪唽
鈹?  鈹溾攢鈹€ releases/           # 鍙戝竷璇存槑
鈹?  鈹斺攢鈹€ redesign/           # v2.2 閲嶆瀯璁捐鏂囨。锛?0-baseline / 01-audit / 02-strategy / 03-tokens / 04-prod-worktree锛?
鈹溾攢鈹€ tests/                  # pytest
鈹?  鈹溾攢鈹€ conftest.py
鈹?  鈹溾攢鈹€ pytest.ini
鈹?  鈹溾攢鈹€ test_*.py           # 涓氬姟涓庨€氶亾娴嬭瘯
鈹?  鈹斺攢鈹€ test_web_assets.py  # 鍓嶇 ESM 瑙ｆ瀽 + vendor/CDN 瀹堝崼锛坴2.2 鏂板锛?
鈹溾攢鈹€ ops/                    # 鍚姩 / 閮ㄧ讲 / 鏋勫缓 / 涓€娆℃€ц剼鏈?
鈹?  鈹溾攢鈹€ start.bat / start.sh             # 鏈満鍚姩鑴氭湰
鈹?  鈹溾攢鈹€ start-docker-win.ps1 / start-docker-wsl.sh
鈹?  鈹溾攢鈹€ Dockerfile
鈹?  鈹溾攢鈹€ docker-compose.yml / docker-compose.windows.yml
鈹?  鈹溾攢鈹€ docker-entrypoint.sh
鈹?  鈹溾攢鈹€ requirements/{base.txt, dev.txt}
鈹?  鈹溾攢鈹€ scripts/backup-db.py             # 鎵嬪姩鎷?db 蹇収
鈹?  鈹溾攢鈹€ scripts/copy-dev-to-prod.py      # dev 鈫?prod 閰嶇疆澶嶅埗
鈹?  鈹斺攢鈹€ scripts/oneoff/                  # 涓€娆℃€у垎鏋愪笌鍥炲～鑴氭湰锛堝綊妗ｏ紱涓嶈 import锛?
鈹溾攢鈹€ data/                   # 杩愯鏃舵暟鎹紙DB + 鍑嵁锛?gitignore锛?
鈹溾攢鈹€ config.example.toml     # 閰嶇疆妯℃澘锛坈onfig.toml 鑷韩琚?gitignore锛?
鈹斺攢鈹€ README.md / README_EN.md / SECURITY.md / LICENSE / .gitignore / .dockerignore / .mailmap
```


鍚姩鑴氭湰锛?

```powershell
# Windows
.\ops\start.bat
# Linux / macOS
chmod +x ops/start.sh && ./ops/start.sh
```

Docker 鍚姩锛?

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File .\ops\start-docker-win.ps1
# WSL
./ops/start-docker-wsl.sh
```

## v2.2 鏇存柊鍐呭

鐩稿 1.4 / 2.0 / 2.1 鐨勪富瑕佸彉鍖栵細

- **GMI 閫氶亾**锛氭柊澧?opt-in 閫氶亾锛圤penAI 鍏煎锛岃蛋 Web 瀵煎叆 API Key锛夈€備笉鍦ㄩ粯璁ら€氶亾鍒楄〃閲岋紝鍚敤闇€鍦?`CB_GATEWAY_PROVIDERS` 鏈熬杩藉姞 `gmi`銆?
- **绠＄悊椤?vendor 鏈湴鍖?*锛歏ue 3.4.21 涓?SortableJS 1.15.6 浠?jsdelivr CDN 钀藉埌 `web/vendor/`锛岀敱 FastAPI StaticFiles 鐩存帴鏈嶅姟銆傛柇缃戜粛鍙墦寮€绠＄悊椤点€俙tests/test_web_assets.py` 瀹堝崼 CDN 寮曠敤姘镐笉鍥炲綊銆?
- **鍚庣涓夊法鐭虫ā鍧楁媶鍒?*锛?
  - `storage/database.py` 閫€鍖栦负 re-export 鍏煎闂ㄩ潰锛屽瓙妯″潡鍦?`storage/repos/{accounts, api_keys, logs, settings, stats, _common}.py`銆?
  - `gateway/server.py` 鐣?app 宸ュ巶銆乴ifespan銆丼taticFiles 鎸傝浇锛涚鐐规寜鍩熸媶鍒?`gateway/routers/{admin.py, v1.py, static_router.py}`锛涘叡浜壌鏉冧緷璧栨敹鍙ｅ埌 `gateway/deps.py`銆?
  - `upstream/proxy.py` 鐣?pipeline 涓绘祦绋嬶紱妯″瀷鍒悕銆佸鏍搞€佺簿绠€銆丷esponses 缈昏瘧鎷嗗埌 `upstream/{aliases.py, moderation.py, compaction.py, responses.py}`銆?
  - 56 涓鐐硅矾寰勩€佸绾︺€佽涓哄叏閮ㄤ繚鎸佷笉鍙橈紱`pytest` 涓庡熀绾夸竴鑷达紙pre-existing 澶辫触涓嶅湪閲嶆瀯鑼冨洿锛夈€?
- **绠＄悊椤?Overhaul**锛? 涓?lever锛堜緷璧栨湰鍦板寲銆佺増鏈彿鍗曚竴鏉ユ簮銆丆SS 鍗曚竴浠ょ墝浣撶郴閲嶅缓銆佺粍浠跺眰閲嶅仛銆佸浘琛ㄤ护鐗屽寲銆侀噸鐐归〉閲嶆帓銆佺Щ鍔ㄧ鏂偣鏀舵暃銆佷竴娆℃€ц剼鏈綊妗ｏ級銆傜増鏈彿鐜板湪浠?`/admin/meta` 鎷夛紝涓嶅啀鍐欐銆俙em-dash` 鍏ㄩ儴娓呯悊涓轰腑鏂囨爣鐐广€?
- **涓€娆?commits 璧板畬**锛氭瘡涓?lever 涓€涓?commit锛坄refactor(web): ...` / `refactor(storage): ...` / `refactor(gateway): ...` / `refactor(upstream): ...`锛夛紝鎵€鏈?commit 宸?push 鍒?`refactor/web-console-ia`銆傝缁嗚璁¤ `docs/redesign/`銆?
- **閰嶇疆鏂囦欢 `config.toml`**锛氭柊澧炪€俙gateway.server` 鍚姩鏃惰嚜鍔ㄥ姞杞斤紝鏀寔 `[default]` / `[dev]` / `[prod]` profile 涓?`--config <profile>` / `CB_GATEWAY_CONFIG=<profile>` 鍒囨崲銆俤ev / prod 鍙?checkout 鍚勮嚜涓€浠?`config.toml`锛坄.gitignore`d锛宲er-deploy 绉佹湁锛夛紝绔彛鍜?db 璺緞宸插啓姝伙紝bare `python -m src.gateway.server` 璧板绔€傝瑙?[閰嶇疆鏂囦欢](#閰嶇疆鏂囦欢) 涓€鑺傘€?

## License

MIT
