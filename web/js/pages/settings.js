import {api,apiErr} from '../api.js';
import {I} from '../icons.js';
const{ref,reactive,computed,onMounted}=Vue;

export default {props:['token','toast','saveToken'],setup(p){
  const defaults={backend_url:'https://copilot.tencent.com',default_domain:'www.codebuddy.cn',timeout:300};
  const s=ref({...defaults,base_url:'http://127.0.0.1:8787/v1',admin_auth:'本机 Cookie 自动验证'}),ld=ref(true),saving=ref(false),adminToken=ref(p.token||'');
  async function load(){ld.value=true;try{const cfg=await api.get('/admin/settings',p.token);s.value={...s.value,...cfg}}catch(e){p.toast(apiErr(e,'加载失败'),'err')}ld.value=false}
  async function save(){if(saving.value)return;saving.value=true;try{await api.put('/admin/settings',{backend_url:s.value.backend_url,default_domain:s.value.default_domain,timeout:Number(s.value.timeout)||300},p.token);p.toast('已保存')}catch(e){p.toast(apiErr(e,'保存失败'),'err')}saving.value=false}
  function resetDefaults(){s.value={...s.value,backend_url:defaults.backend_url,default_domain:defaults.default_domain,timeout:defaults.timeout};p.toast('已恢复默认','info')}
  async function saveAdminToken(){
    const t=adminToken.value.trim();
    p.saveToken(t);
    try{const r=await fetch('/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:t}),credentials:'same-origin'});
      if(r.ok){p.toast('登录成功，Cookie 已保存')}else{p.toast(r.status===401?'Admin Token 不正确':'登录失败：'+r.status,'err')}
    }catch(e){p.toast('登录请求失败：'+e.message,'err')}
  }
  async function clearAdminToken(){adminToken.value='';p.saveToken('');try{await fetch('/admin/logout',{method:'POST',credentials:'same-origin'})}catch(e){}p.toast('已清除','info')}
  onMounted(load);return{s,ld,saving,adminToken,load,save,resetDefaults,saveAdminToken,clearAdminToken,I}
},template:`
<div>
  <div class="phead"><h1>设置</h1><p>运行配置与管理鉴权（客户端接入请前往「接入指南」页）</p></div>
  <div class="tbar"><button class="btn s pri" @click="save" :disabled="saving||ld">{{saving?'保存中':'保存配置'}}</button><button class="btn s" @click="resetDefaults" :disabled="saving||ld">恢复默认</button><button class="btn s" @click="load" :disabled="ld"><span v-html="I.refresh"></span>{{ld?'刷新中':'刷新'}}</button></div>
  <div v-if="ld" class="load"><div class="spin"></div></div>
  <template v-else>
    <div class="card">
      <div class="card-h">后端参数<span class="sub">保存后对后续请求生效</span></div>
      <div class="card-p form-grid">
        <div class="field"><label>后端地址</label><input v-model="s.backend_url" placeholder="https://copilot.tencent.com"/><div class="hint">上游 Work Buddy 服务地址。</div></div>
        <div class="field"><label>默认域名</label><input v-model="s.default_domain" placeholder="www.codebuddy.cn"/><div class="hint">账号 auth 中没有 domain 时使用。</div></div>
        <div class="field"><label>请求超时（秒）</label><input v-model="s.timeout" type="number" min="30" max="900" placeholder="300"/><div class="hint">长上下文或慢模型建议保持 300。</div></div>
        <div class="field"><label>管理鉴权</label><input v-model="s.admin_auth" disabled/><div class="hint">本机 Web UI 自动使用 HttpOnly Cookie。</div></div>
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
