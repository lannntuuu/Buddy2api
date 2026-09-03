import {api,apiErr} from '../api.js';
import {I} from '../icons.js';
const{ref,reactive,computed,onMounted}=Vue;

export default {props:['token','toast'],setup(p){
  const s=ref({base_url:'http://127.0.0.1:8787/v1',data_file:'codebuddy_gateway.db'}),ld=ref(true),copied=ref(''),setupTab=ref('codex');
  const keysList=ref([]),codexKeyInput=ref(''),codexStatus=ref(null),codexBusy=ref(false),codexResult=ref(null);
  const presets={
    codex:{name:'Codex (OpenAI)',base:'http://127.0.0.1:8787/v1',model:'auto',note:'Codex 强制使用 Responses API (wire_api="responses")。本网关已内置协议转换层，自动将 /v1/responses 映射到后端 Chat Completions。模型别名、内容清洗均自动启用。'},
    opencode:{name:'OpenCode / OpenClaw',base:'http://127.0.0.1:8787/v1',model:'auto',note:'运行在宿主机上的客户端直接使用 127.0.0.1。'},
    sub2api:{name:'sub2api Docker',base:'http://host.docker.internal:8787/v1',model:'auto',note:'sub2api 在 Docker 容器内访问宿主机时使用 host.docker.internal，不要填 localhost。'},
    cherry:{name:'Cherry Studio',base:'http://127.0.0.1:8787/v1',model:'auto',note:'供应商类型选择 OpenAI Compatible，Key 使用 API Keys 页面创建的 sk-cb。'},
    nextchat:{name:'NextChat',base:'http://127.0.0.1:8787/v1',model:'auto',note:'自定义接口地址填写到 /v1，模型填 auto 或「通道与模型」页中的别名。'},
    generic:{name:'通用 OpenAI Compatible',base:'http://127.0.0.1:8787/v1',model:'auto',note:'支持 /v1/chat/completions、/v1/responses 和 /v1/models；Base URL 填写到 /v1。'},
    curl:{name:'curl',base:'http://127.0.0.1:8787/v1',model:'auto',note:'用于快速验证服务、Key 和模型映射是否正常。'},
  };
  async function load(){ld.value=true;try{const cfg=await api.get('/admin/settings',p.token);s.value={...s.value,...cfg};keysList.value=await api.get('/admin/api-keys',p.token);if(keysList.value.length&&!codexKeyInput.value)codexKeyInput.value=keysList.value.find(k=>k.key)?.key||'';try{codexStatus.value=await api.get('/admin/codex/status',p.token)}catch(e){}}catch(e){p.toast(apiErr(e,'加载失败'),'err')}ld.value=false}
  async function codexSetup(){
    if(codexBusy.value)return;
    if(!codexKeyInput.value||!codexKeyInput.value.startsWith('sk-cb-')){p.toast('请输入有效的 sk-cb- 开头的 API Key','err');return}
    codexBusy.value=true;codexResult.value=null;
    try{
      const res=await api.post('/admin/codex/setup',{api_key:codexKeyInput.value.trim()},p.token);
      codexResult.value=res;
      if(res.status==='ok'){p.toast('Codex 配置已写入','ok');try{codexStatus.value=await api.get('/admin/codex/status',p.token)}catch(e){}}
      else{p.toast('配置失败','err')}
    }catch(e){p.toast(apiErr(e,'配置失败'),'err');codexResult.value={status:'error',message:String(e)}}
    codexBusy.value=false;
  }
  function copy(v,name){navigator.clipboard.writeText(v);copied.value=name;p.toast('已复制','info');setTimeout(()=>copied.value='',1200)}
  const currentPreset=computed(()=>presets[setupTab.value]||presets.codex)
  function envBlock(){
    if(setupTab.value==='codex'){
      return `# ~/.codex/config.toml\nmodel = "auto"\nmodel_provider = "buddy2api"\n\n[model_providers.buddy2api]\nname = "Buddy2api"\nbase_url = "http://127.0.0.1:8787/v1"\nwire_api = "responses"\nenv_key = "OPENAI_API_KEY"\n\n# ~/.codex/auth.json\n{"OPENAI_API_KEY":"YOUR_SK_CB_KEY"}`
    }
    const x=currentPreset.value;return `OPENAI_BASE_URL=${x.base}\nOPENAI_API_KEY=YOUR_SK_CB_KEY\nOPENAI_MODEL=${x.model}`
  }
  function curlBlock(){
    if(setupTab.value==='codex'){
      return `# 测试 Responses API（Codex 使用的端点）\ncurl http://127.0.0.1:8787/v1/responses \\\n  -H "Authorization: Bearer YOUR_SK_CB_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model":"auto","input":"hi","stream":false}'`
    }
    const x=currentPreset.value;return `curl ${x.base}/chat/completions \\\n  -H "Authorization: Bearer YOUR_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model":"${x.model}","messages":[{"role":"user","content":"hi"}]}'`
  }
  onMounted(load);return{s,ld,copied,setupTab,presets,currentPreset,envBlock,curlBlock,load,copy,codexSetup,keysList,codexKeyInput,codexStatus,codexBusy,codexResult}
},template:`
<div>
  <div class="phead"><h1>接入指南</h1><p>客户端接入唯一参考 · Base URL / 模型 / 各客户端配置模板 / Codex 一键配置</p></div>
  <div v-if="ld" class="load"><div class="spin"></div></div>
  <template v-else>
    <div class="card">
      <div class="card-h">接入信息<span class="sub">OpenAI Compatible</span></div>
      <div class="card-p info-list">
        <div class="info-row"><div class="k">Base URL</div><div class="v">{{s.base_url}}</div><button class="btn s" @click="copy(s.base_url,'base')">{{copied==='base'?'已复制':'复制'}}</button></div>
        <div class="info-row"><div class="k">API Key</div><div class="v">在 API Keys 页面创建 sk-cb-...</div><span class="tag">Bearer</span></div>
        <div class="info-row"><div class="k">Models</div><div class="v">auto, glm-5.2, kimi-k2.7, deepseek-v4-pro</div><button class="btn s" @click="copy('auto','model')">{{copied==='model'?'已复制':'复制'}}</button></div>
        <div class="info-row"><div class="k">Database</div><div class="v">{{s.data_file}}</div><span class="tag">SQLite</span></div>
      </div>
    </div>

    <div class="card">
      <div class="card-h">客户端接入向导<span class="sub">按运行位置选择地址</span></div>
      <div class="card-p">
        <div class="callout" style="margin-bottom:12px">宿主机客户端用 <span class="mono">http://127.0.0.1:8787/v1</span>；Docker 容器里的 sub2api 用 <span class="mono">http://host.docker.internal:8787/v1</span>。Codex 选择 "Codex (OpenAI)" 标签查看专用配置（wire_api、内容清洗等自动处理）。</div>
        <div class="setup-tabs"><button v-for="(x,k) in presets" :key="k" :class="{on:setupTab===k}" @click="setupTab=k">{{x.name}}</button></div>
        <div class="setup-panel">
          <div class="info-list">
            <div class="info-row"><div class="k">Base URL</div><div class="v">{{currentPreset.base}}</div><button class="btn s" @click="copy(currentPreset.base,'preset-base')">{{copied==='preset-base'?'已复制':'复制'}}</button></div>
            <div class="info-row"><div class="k">API Key</div><div class="v">YOUR_SK_CB_KEY</div><span class="tag">Bearer</span></div>
            <div class="info-row"><div class="k">Model</div><div class="v">{{currentPreset.model}}</div><button class="btn s" @click="copy(currentPreset.model,'preset-model')">{{copied==='preset-model'?'已复制':'复制'}}</button></div>
            <div class="callout">{{currentPreset.note}}</div>
          </div>
          <div>
            <div class="codeblk" style="margin-bottom:10px">{{envBlock()}}</div>
            <button class="btn s" @click="copy(envBlock(),'preset-env')" style="margin-bottom:10px">{{copied==='preset-env'?'已复制':'复制配置'}}</button>
            <div class="codeblk">{{curlBlock()}}</div>
            <button class="btn s" @click="copy(curlBlock(),'preset-curl')" style="margin-top:10px">{{copied==='preset-curl'?'已复制':'复制 curl'}}</button>
          </div>
        </div>
        <div v-if="setupTab==='codex'" class="callout" style="margin-top:16px;background:var(--blue-soft);border-color:var(--blue-border)">
          <div style="font-weight:600;margin-bottom:8px">Codex 专用处理（自动启用，无需手动配置）</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:12px">
            <div><strong>Responses API 转换</strong><br/><span style="color:var(--fg2)">/v1/responses 请求自动转换为 Chat Completions 转发后端，响应再映射回 Responses SSE 事件格式</span></div>
            <div><strong>模型别名映射</strong><br/><span style="color:var(--fg2)">gpt-5.5 / gpt-5.4 / o3 / o4-mini 等自动映射到 glm-5.2 / deepseek-v4-pro 等后端模型</span></div>
            <div><strong>内容清洗</strong><br/><span style="color:var(--fg2)">Codex system prompt 中的 sandbox / filesystem / execute / elevated 等敏感词自动替换，避免触发腾讯内容审核</span></div>
            <div><strong>工具过滤</strong><br/><span style="color:var(--fg2)">非 function 类型工具（web_search / file_search 等）自动过滤，developer 角色映射为 system</span></div>
          </div>
        </div>
        <div v-if="setupTab==='codex'" style="margin-top:16px;border:1px solid var(--blue-border);border-radius:var(--r);padding:16px;background:var(--blue-soft)">
          <div style="font-weight:600;font-size:14px;margin-bottom:4px">一键配置 Codex</div>
          <div style="font-size:12px;color:var(--fg2);margin-bottom:16px">选择一个 API Key，自动写入 <span class="mono">~/.codex/config.toml</span> 和 <span class="mono">~/.codex/auth.json</span>（原文件备份为 .bak），并设置 <span class="mono">OPENAI_API_KEY</span> 环境变量。配置后需完全关闭 Codex 重新打开。</div>

          <div style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;margin-bottom:12px">
            <div class="field" style="flex:1;min-width:280px;margin:0">
              <label>API Key（sk-cb- 开头）</label>
              <input v-model="codexKeyInput" placeholder="sk-cb-xxxxxxxxxxxxxxxx" style="width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:var(--r);font-size:13px;font-family:var(--mono);background:var(--bg)"/>
              <div class="hint" style="margin-top:4px">可在 API Keys 页面随时查看并复制完整 Key。</div>
            </div>
            <button class="btn pri" @click="codexSetup" :disabled="codexBusy||!codexKeyInput" style="white-space:nowrap">{{codexBusy?'写入中...':'一键写入配置'}}</button>
          </div>

          <div v-if="codexStatus" style="margin-top:12px">
            <div style="font-size:12px;font-weight:600;margin-bottom:6px">当前 Codex 配置状态</div>
            <div style="display:flex;gap:8px;flex-wrap:wrap">
              <span class="badge" :class="codexStatus.config_has_buddy2api?'ok':'inactive'">Provider: {{codexStatus.config_has_buddy2api?'buddy2api':'未配置'}}</span>
              <span class="badge" :class="codexStatus.config_wire_api==='responses'?'ok':'warn'">wire_api: {{codexStatus.config_wire_api||'未设置'}}</span>
              <span class="badge" :class="codexStatus.auth_has_key?'ok':'inactive'">auth.json: {{codexStatus.auth_has_key?'已配置':'未配置'}}</span>
              <span class="badge" :class="codexStatus.config_model?'ok':'inactive'">model: {{codexStatus.config_model||'未设置'}}</span>
            </div>
          </div>

          <div v-if="codexResult" style="margin-top:12px;padding:12px;border-radius:var(--r);background:var(--bg);border:1px solid var(--border)">
            <div v-if="codexResult.status==='ok'" style="color:var(--green)">
              <strong>配置成功</strong>
              <div style="margin-top:6px;font-size:12px;color:var(--fg2)">已写入文件：<span class="mono" v-for="f in codexResult.written" :key="f">{{f}} </span></div>
              <div v-if="codexResult.backed_up && codexResult.backed_up.length" style="margin-top:4px;font-size:12px;color:var(--fg3)">已备份：<span class="mono" v-for="f in codexResult.backed_up" :key="f">{{f}} </span></div>
              <div style="margin-top:8px;font-size:12px;color:var(--blue)">请完全关闭 Codex 后重新打开。</div>
            </div>
            <div v-else style="color:var(--red)">
              <strong>配置失败</strong>
              <div style="margin-top:4px;font-size:12px">{{codexResult.message||'未知错误'}}</div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="card card-p"><div style="font-weight:600;font-size:13px;margin-bottom:10px">快速验证</div>
      <div class="codeblk"><span class="k">curl</span> {{s.base_url}}/chat/completions \\
  -H <span class="s">"Authorization: Bearer YOUR_KEY"</span> \\
  -H <span class="s">"Content-Type: application/json"</span> \\
  -d <span class="s">'{"model":"auto","messages":[{"role":"user","content":"hi"}]}'</span></div>
    </div>
  </template>
</div>`};
