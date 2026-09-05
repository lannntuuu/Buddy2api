import {api,apiErr} from '../api.js';
import {I} from '../icons.js';
const{ref,reactive,onMounted}=Vue;

export default {props:['token','toast','saveToken'],setup(p){
  const defaults={backend_url:'https://copilot.tencent.com',default_domain:'www.codebuddy.cn',timeout:300};
  const s=ref({...defaults,base_url:'http://127.0.0.1:8787/v1',admin_auth:'本机 Cookie 自动验证'}),ld=ref(true),saving=ref(false),adminToken=ref(p.token||'');
  const hostOverrides=reactive({qwenwork:{gateway:''},qclaw:{jprx_gateway:'',aizone_base:''},traesolo:{oauth_host:'',console_host:'',agent_host:''},traework:{agent_host:'',ug_host:''}});

  function fillHosts(){
    const ch=s.value.channel_hosts||{};
    hostOverrides.qwenwork.gateway=ch.qwenwork?.gateway||'';
    hostOverrides.qclaw.jprx_gateway=ch.qclaw?.jprx_gateway||'';
    hostOverrides.qclaw.aizone_base=ch.qclaw?.aizone_base||'';
    hostOverrides.traesolo.oauth_host=ch.traesolo?.oauth_host||'';
    hostOverrides.traesolo.console_host=ch.traesolo?.console_host||'';
    hostOverrides.traesolo.agent_host=ch.traesolo?.agent_host||'';
    hostOverrides.traework.agent_host=ch.traework?.agent_host||'';
    hostOverrides.traework.ug_host=ch.traework?.ug_host||'';
  }
  async function load(){ld.value=true;try{const cfg=await api.get('/admin/settings',p.token);s.value={...s.value,...cfg};fillHosts()}catch(e){p.toast(apiErr(e,'加载失败'),'err')}ld.value=false}
  async function save(){if(saving.value)return;saving.value=true;try{await api.put('/admin/settings',{backend_url:s.value.backend_url,default_domain:s.value.default_domain,timeout:Number(s.value.timeout)||300,channel_hosts:{qwenwork:{gateway:hostOverrides.qwenwork.gateway.trim()},qclaw:{jprx_gateway:hostOverrides.qclaw.jprx_gateway.trim(),aizone_base:hostOverrides.qclaw.aizone_base.trim()},traesolo:{oauth_host:hostOverrides.traesolo.oauth_host.trim(),console_host:hostOverrides.traesolo.console_host.trim(),agent_host:hostOverrides.traesolo.agent_host.trim()},traework:{agent_host:hostOverrides.traework.agent_host.trim(),ug_host:hostOverrides.traework.ug_host.trim()}}},p.token);p.toast('已保存')}catch(e){p.toast(apiErr(e,'保存失败'),'err')}saving.value=false}
  function resetDefaults(){s.value={...s.value,backend_url:defaults.backend_url,default_domain:defaults.default_domain,timeout:defaults.timeout};p.toast('已恢复默认','info')}
  async function saveAdminToken(){
    const t=adminToken.value.trim();
    p.saveToken(t);
    try{const r=await fetch('/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:t}),credentials:'same-origin'});
      if(r.ok){p.toast('登录成功，Cookie 已保存')}else{p.toast(r.status===401?'Admin Token 不正确':'登录失败：'+r.status,'err')}
    }catch(e){p.toast('登录请求失败：'+e.message,'err')}
  }
  async function clearAdminToken(){adminToken.value='';p.saveToken('');try{await fetch('/admin/logout',{method:'POST',credentials:'same-origin'})}catch(e){}p.toast('已清除','info')}

  onMounted(async ()=>{await load()});

  return{s,ld,saving,adminToken,hostOverrides,load,save,resetDefaults,saveAdminToken,clearAdminToken,I}
},template:`
<div>
<div class="phead"><h1>设置</h1><p>运行配置与管理鉴权（客户端接入请前往「接入指南」页）</p></div>
<div class="tbar"><button class="btn s pri" @click="save" :disabled="saving||ld">{{saving?'保存中':'保存配置'}}</button><button class="btn s" @click="resetDefaults" :disabled="saving||ld">恢复默认</button><button class="btn s" @click="load" :disabled="ld"><span v-html="I.refresh"></span>{{ld?'刷新中':'刷新'}}</button></div>
<div v-if="ld" class="load"><div class="spin"></div></div>
<template v-else>
  <div class="card">
    <div class="card-h">后端参数<span class="sub">保存后对后续请求生效</span></div>
    <div class="card-p form-grid">
      <div class="field"><label>后端地址</label><input v-model="s.backend_url" placeholder="https://copilot.tencent.com"/><div class="hint">上游 WorkBuddy 服务地址。</div></div>
      <div class="field"><label>默认域名</label><input v-model="s.default_domain" placeholder="www.codebuddy.cn"/><div class="hint">账号 auth 中没有 domain 时使用。</div></div>
      <div class="field"><label>请求超时（秒）</label><input v-model="s.timeout" type="number" min="30" max="900" placeholder="300"/><div class="hint">长上下文或慢模型建议保持 300。</div></div>
      <div class="field"><label>管理鉴权</label><input v-model="s.admin_auth" disabled/><div class="hint">本机 Web UI 自动使用 HttpOnly Cookie。</div></div>
      <div class="field"><label>QwenWork 网关</label>
        <input v-model="hostOverrides.qwenwork.gateway" placeholder="留空使用默认 https://gateway.qwenwork.cn"/>
        <div class="hint">自定义 QwenWork 网关地址。留空 = 默认。</div></div>
      <div class="hint" style="grid-column:1/-1">GMI / 阿里百炼 Base URL 改去「通道管理 → 详情 → 编辑」修改对应通道定义；本页不再托管。通道开关也已搬到「通道管理」主列表。</div>
    </div>
    <div class="card-p">
      <details>
        <summary class="text-bold mb-2" style="cursor:pointer">高级平台覆盖（QClaw / Trae SOLO / TraeWork）</summary>
        <div class="form-grid">
          <div class="field"><label>QClaw JPRX 网关</label>
            <input v-model="hostOverrides.qclaw.jprx_gateway" placeholder="留空使用默认 https://jprx.m.qq.com"/>
            <div class="hint">业务/签名网关。留空 = 默认。</div></div>
          <div class="field"><label>QClaw AIZone 地址</label>
            <input v-model="hostOverrides.qclaw.aizone_base" placeholder="留空使用默认 https://mmgrcalltoken.3g.qq.com/aizone/v1"/>
            <div class="hint">chat 端点。留空 = 默认。</div></div>
          <div class="field"><label>Trae SOLO OAuth 地址</label>
            <input v-model="hostOverrides.traesolo.oauth_host" placeholder="留空使用默认 https://api.trae.com.cn"/>
            <div class="hint">ExchangeToken / GetUserInfo。留空 = 默认。</div></div>
          <div class="field"><label>Trae SOLO 登录页</label>
            <input v-model="hostOverrides.traesolo.console_host" placeholder="留空使用默认 https://www.trae.cn"/>
            <div class="hint">授权登录页。留空 = 默认。</div></div>
          <div class="field"><label>Trae SOLO Agent 网关</label>
            <input v-model="hostOverrides.traesolo.agent_host" placeholder="留空使用默认 https://trae-api-cn.mchost.guru"/>
            <div class="hint">chat / models。留空 = 默认。</div></div>
          <div class="field"><label>TraeWork Agent 网关</label>
            <input v-model="hostOverrides.traework.agent_host" placeholder="留空使用默认 https://trae-api-cn.mchost.guru"/>
            <div class="hint">chat sessions。留空 = 默认。</div></div>
          <div class="field"><label>TraeWork 积分地址</label>
            <input v-model="hostOverrides.traework.ug_host" placeholder="留空使用默认 https://api.trae.cn"/>
            <div class="hint">签到/积分。留空 = 默认。</div></div>
        </div>
      </details>
    </div>
  </div>

  <div class="card">
    <div class="card-h">管理页登录<span class="sub">粘贴启动日志中的 Admin Token 登录</span></div>
    <div class="card-p">
      <div class="field">
        <label>Admin Token</label>
        <input v-model="adminToken" type="password" placeholder="粘贴启动日志中的 Admin Token"/>
        <div class="hint">保存后会调用 /admin/login 下发 HttpOnly Cookie；同时保留在当前浏览器中作为备用。</div>
      </div>
      <div class="status-line" style="margin-top:10px"><button class="btn s pri" @click="saveAdminToken">保存凭证</button><button class="btn s" @click="clearAdminToken" :disabled="!adminToken&&!token">清除</button></div>
    </div>
  </div>
</template>
</div>`};