import {api,apiErr} from '../api.js';
import {I} from '../icons.js';
const{ref,reactive,computed,onMounted,onBeforeUnmount,nextTick,watch}=Vue;

export default {props:['token','toast','saveToken'],setup(p){
  const defaults={backend_url:'https://copilot.tencent.com',default_domain:'www.codebuddy.cn',timeout:300};
  const s=ref({...defaults,base_url:'http://127.0.0.1:8787/v1',admin_auth:'本机 Cookie 自动验证'}),ld=ref(true),saving=ref(false),adminToken=ref(p.token||'');
  const hostOverrides=reactive({gmi:{base_url:''},qwenwork:{gateway:''},qclaw:{jprx_gateway:'',aizone_base:''},traesolo:{oauth_host:'',console_host:'',agent_host:''},traework:{agent_host:'',ug_host:''}});

  // Channels panel -- collapsed by default. Native <details> for a11y.
  const chs=ref([]),chLd=ref(false),chBusy=ref(false),chErr=ref(''),
        chEnvLocked=ref(false),chOpen=ref(false),chDirty=ref(false),
        chListEl=ref(null);
  let sortable=null;

  function fillHosts(){
    const ch=s.value.channel_hosts||{};
    hostOverrides.gmi.base_url=ch.gmi?.base_url||'';
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
  async function save(){if(saving.value)return;saving.value=true;try{await api.put('/admin/settings',{backend_url:s.value.backend_url,default_domain:s.value.default_domain,timeout:Number(s.value.timeout)||300,channel_hosts:{gmi:{base_url:hostOverrides.gmi.base_url.trim()},qwenwork:{gateway:hostOverrides.qwenwork.gateway.trim()},qclaw:{jprx_gateway:hostOverrides.qclaw.jprx_gateway.trim(),aizone_base:hostOverrides.qclaw.aizone_base.trim()},traesolo:{oauth_host:hostOverrides.traesolo.oauth_host.trim(),console_host:hostOverrides.traesolo.console_host.trim(),agent_host:hostOverrides.traesolo.agent_host.trim()},traework:{agent_host:hostOverrides.traework.agent_host.trim(),ug_host:hostOverrides.traework.ug_host.trim()}}},p.token);p.toast('已保存')}catch(e){p.toast(apiErr(e,'保存失败'),'err')}saving.value=false}
  function resetDefaults(){s.value={...s.value,backend_url:defaults.backend_url,default_domain:defaults.default_domain,timeout:defaults.timeout};p.toast('已恢复默认','info')}
  async function saveAdminToken(){
    const t=adminToken.value.trim();
    p.saveToken(t);
    try{const r=await fetch('/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:t}),credentials:'same-origin'});
      if(r.ok){p.toast('登录成功，Cookie 已保存')}else{p.toast(r.status===401?'Admin Token 不正确':'登录失败：'+r.status,'err')}
    }catch(e){p.toast('登录请求失败：'+e.message,'err')}
  }
  async function clearAdminToken(){adminToken.value='';p.saveToken('');try{await fetch('/admin/logout',{method:'POST',credentials:'same-origin'})}catch(e){}p.toast('已清除','info')}

  async function loadChannels(){
    chLd.value=true;chErr.value='';
    try{
      const r=await api.get('/admin/channels',p.token);
      // The 启用通道 panel shows ALL known channels so the admin can re-enable
      // a previously disabled one. Channels with loaded=false (no provider
      // registered) are still listed so the admin can see what's available.
      chs.value=(r.channels||[]).map(c=>({...c}));
      chEnvLocked.value=!!r.env_locked;
      chDirty.value=false;
      await nextTick();
      initSortable();
    }catch(e){chErr.value=apiErr(e,'加载通道失败')}
    chLd.value=false;
  }
  function toggleChannel(id, on){
    const c=chs.value.find(x=>x.id===id);if(c)c.enabled=on;
    chDirty.value=true;
  }

  function initSortable(){
    if(sortable){sortable.destroy();sortable=null}
    if(!chListEl.value||typeof Sortable==='undefined')return;
    sortable=Sortable.create(chListEl.value, {
      animation:150,
      ghostClass:'chk-ghost',
      dragClass:'chk-drag',
      // The whole .chk-row is the drag handle. workbuddy is locked to the top:
      // refuse any move that touches it (either as the dragged element or the
      // drop target).
      onMove(evt){
        const draggedId=evt.dragged?.dataset?.cid;
        const relatedId=evt.related?.dataset?.cid;
        if(draggedId==='workbuddy')return false;
        if(relatedId==='workbuddy')return false;
        return true;
      },
      onEnd(){
        const ids=[...chListEl.value.querySelectorAll('.chk-row')].map(el=>el.dataset.cid);
        const map=new Map(chs.value.map(c=>[c.id,c]));
        chs.value=ids.map(id=>map.get(id)).filter(Boolean);
        chDirty.value=true;
      },
    });
  }

  async function saveChannels(){
    if(chBusy.value||chEnvLocked.value||!chDirty.value)return;
    chBusy.value=true;
    const ids=chs.value.filter(c=>c.enabled).map(c=>c.id);
    const order=chs.value.map(c=>c.id);
    try{
      const r=await api.put('/admin/channels',{enabled:ids,order:order},p.token);
      chs.value.forEach(c=>{c.enabled=(r.enabled||[]).includes(c.id)});
      chDirty.value=false;
      p.toast('通道已保存');
    }catch(e){p.toast(apiErr(e,'保存失败'),'err')}
    chBusy.value=false;
  }

  const chSummary=computed(()=>{
    const names=chs.value.filter(c=>c.enabled).map(c=>c.display_name||c.id);
    if(!names.length)return '未启用任何通道';
    if(names.length<=3)return names.join(' · ');
    return names.slice(0,3).join(' · ')+' · +'+(names.length-3)+' 个';
  });

  onMounted(async ()=>{await load();await loadChannels()});
  onBeforeUnmount(()=>{if(sortable){sortable.destroy();sortable=null}});

  // Re-bind sortable when the details panel toggles open. The DOM nodes exist
  // either way, but we want a guarantee that the instance is fresh after the
  // browser re-lays out the children.
  watch(chOpen, async (open)=>{if(open){await nextTick();initSortable()}});

  return{s,ld,saving,adminToken,hostOverrides,load,save,resetDefaults,saveAdminToken,clearAdminToken,
         chs,chLd,chBusy,chEnvLocked,chErr,chOpen,chDirty,chSummary,chListEl,
         loadChannels,toggleChannel,saveChannels,I}
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
        <div class="field"><label>GMI Cloud Base URL</label>
          <input v-model="hostOverrides.gmi.base_url" placeholder="留空使用默认 https://api.gmi-serving.com/v1"/>
          <div class="hint">自定义 GMI 上游地址（镜像/反代）。留空 = 默认。</div></div>
        <div class="field"><label>QwenWork 网关</label>
          <input v-model="hostOverrides.qwenwork.gateway" placeholder="留空使用默认 https://gateway.qwenwork.cn"/>
          <div class="hint">自定义 QwenWork 网关地址。留空 = 默认。</div></div>
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

    <details class="card ch-card" :open="chOpen" @toggle="chOpen=$event.target.open">
      <summary class="card-h ch-h">
        <span class="ch-h-left">
          <span class="ch-h-title">启用通道</span>
          <span class="ch-summary">{{chSummary}}</span>
          <span v-if="chDirty" class="ch-dirty">未保存</span>
        </span>
        <span class="ch-h-right">
          <button class="btn s pri"
                  @click.stop="saveChannels"
                  :disabled="chBusy||chEnvLocked||!chDirty"
                  @mousedown.stop @focus.stop>{{chBusy?'保存中':'保存通道'}}</button>
          <span class="ch-caret" v-html="I.chevron"></span>
        </span>
      </summary>
      <div class="card-p ch-panel">
        <div v-if="chEnvLocked" class="status-line ch-warn">
            检测到环境变量 <code>CB_GATEWAY_PROVIDERS</code>，通道开关为只读；如需在 UI 内调整，请去掉环境变量后重启。
          </div>
        <div v-if="chLd" class="load"><div class="spin"></div></div>
        <div v-else-if="chErr" class="status-line err">{{chErr}}</div>
        <div v-else class="ch-list" ref="chListEl">
          <div v-for="c in chs" :key="c.id" class="chk-row" :data-cid="c.id">
            <input type="checkbox"
                   :checked="c.enabled"
                   :disabled="chEnvLocked||c.id==='workbuddy'||chBusy"
                   @change="toggleChannel(c.id, $event.target.checked)"
                   @click.stop
                   :data-cid="c.id"/>
            <span class="chk-label">
              <span class="chk-name">{{c.display_name}}</span>
              <span class="chk-id">{{c.id}}</span>
            </span>
            <span v-if="c.id==='workbuddy'" class="chk-tag">必选</span>
            <span v-else-if="!c.loaded" class="chk-tag warn">未加载</span>
            <span v-else-if="c.checkin_supported" class="chk-tag">签到</span>
            <span class="chk-grip" aria-hidden="true">⋮⋮</span>
          </div>
        </div>
      </div>
    </details>

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