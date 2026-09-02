import {api,apiErr,fmt,tok} from '../api.js';
import {I} from '../icons.js';
const{ref,reactive,computed,onMounted}=Vue;

export default {props:['token','toast'],setup(p){
  const l=ref([]),ld=ref(true),sa=ref(false),ai=ref(''),nm=ref(''),disc=ref(null),dl=ref(false),scanning=ref(false),adding=ref(false),authPath=ref(''),test=ref(null),tl=ref(0),busy=ref({}),discChannel=ref('workbuddy'),channels=ref([{id:'workbuddy',display_name:'WorkBuddy'},{id:'qclaw',display_name:'QClaw'},{id:'qwenwork',display_name:'QwenWork / 千问办公'},{id:'traework',display_name:'TraeWork'},{id:'traesolo',display_name:'Trae SOLO'},{id:'gmi',display_name:'GMI Cloud'}]);
  // API Key 类通道（单 key、无本机登录文件）：选中时渲染粘贴导入面板而非文件检测 UI。
  const APIKEY_CHANNELS={'gmi':{name:'GMI Cloud',base:'https://api.gmi-serving.com/v1',env:'CB_GMI_API_KEY'}};
  const gmiKey=ref(''),gmiNick=ref(''),gmiBase=ref(''),gmiBusy=ref(false);
  const gmiMode=computed(()=>APIKEY_CHANNELS[discChannel.value]?discChannel.value:'');
  const APIKEY_PANEL=computed(()=>APIKEY_CHANNELS[gmiMode.value]||{name:'',base:'',env:''});
  const solo=reactive({pending:false,url:'',pendingId:'',callbackUrl:'',state:'',uid:'',error:'',manual:''}),soloBusy=ref(false);let soloTimer=null,soloGen=0;
  const filters=reactive({q:'',status:'all',sort:'priority'});
  function hydrate(a){return {...a,_weight:a.weight||1,_priority:a.priority||0,_creditSnapshot:a.credit_snapshot||a.credit_limit||0,_baseWeight:a.weight||1,_basePriority:a.priority||0,_baseCreditSnapshot:a.credit_snapshot||a.credit_limit||0}}
  function dirty(a){return Number(a._weight||1)!==Number(a._baseWeight||1)||Number(a._priority||0)!==Number(a._basePriority||0)||Number(a._creditSnapshot||0)!==Number(a._baseCreditSnapshot||0)}
  function busyKey(id,k){return busy.value[id+'-'+k]}
  async function load(){ld.value=true;try{l.value=(await api.get('/admin/accounts',p.token)).map(hydrate)}catch(e){p.toast(apiErr(e),'err')}ld.value=false}
  async function loadChannels(){try{const ch=await api.get('/admin/channels',p.token);if(ch.channels?.length)channels.value=ch.channels.filter(c=>c.enabled)}catch(e){}}
  async function discover(path=''){dl.value=true;try{const qs=new URLSearchParams();if(path&&path.trim())qs.set('auth_dir',path.trim());if(discChannel.value)qs.set('channel',discChannel.value);const q=qs.toString();disc.value=await api.get('/admin/accounts/discover'+(q?'?'+q:''),p.token)}catch(e){p.toast(apiErr(e,'检测失败'),'err')}dl.value=false}
  async function scan(path=''){if(scanning.value)return;scanning.value=true;try{if(disc.value?.preview_token){const body={channel:disc.value.channel||'workbuddy',preview_token:disc.value.preview_token};if(path&&path.trim())body.auth_dir=path.trim();const r=await api.post('/admin/accounts/import',body,p.token);p.toast('导入 '+r.imported+' · 更新 '+r.updated+' · 跳过 '+r.skipped)}else{const body=path&&path.trim()?{auth_dir:path.trim()}:{};const r=await api.post('/admin/accounts/scan',body,p.token);p.toast('导入 '+r.imported+' · 更新 '+r.updated+' · 跳过 '+r.skipped)}await load();await discover(path)}catch(e){p.toast(apiErr(e,'扫描失败'),'err')}scanning.value=false}
  async function scanCustom(){const path=authPath.value.trim();if(!path){p.toast('请先填写目录或 .info 文件路径','err');return}await scan(path)}
  async function add(){
    if(adding.value)return;
    if(!ai.value.trim()){p.toast('请先粘贴 Auth JSON','err');return}
    let d;
    try{d=JSON.parse(ai.value)}catch(e){
      // 兼容裸粘贴：单个 API Key / "Bearer xxx" / 单行字符串都包成 JSON。
      const raw=ai.value.trim();
      if(raw&&raw.length<4096&&!raw.startsWith('{')&&!raw.startsWith('['))d={api_key:raw};
      else{p.toast('失败:'+e.message,'err');adding.value=false;return}
    }
    adding.value=true;
    try{
      const r=await api.post('/admin/accounts',{...d,name:nm.value,provider:d.provider||d.channel||discChannel.value},p.token);
      p.toast(r.updated?'账号已更新':'添加成功');sa.value=false;ai.value='';nm.value='';await load();await discover(authPath.value)
    }catch(e){p.toast('失败:'+e.message,'err')}adding.value=false
  }
  async function addApiKey(){
    const ch=APIKEY_CHANNELS[gmiMode.value];if(!ch||gmiBusy.value)return;
    if(!gmiKey.value.trim()){p.toast('请先粘贴 '+ch.name+' API Key','err');return}
    gmiBusy.value=true;
    try{
      const body={provider:gmiMode.value,api_key:gmiKey.value.trim()};
      if(gmiNick.value.trim())body.nickname=gmiNick.value.trim();
      if(gmiBase.value.trim())body.base_url=gmiBase.value.trim();
      const r=await api.post('/admin/accounts',body,p.token);
      p.toast((r.updated?'Key 已更新 · ':'导入成功 · ')+(r.uid||''),'ok');
      gmiKey.value='';gmiNick.value='';gmiBase.value='';
      await load();
    }catch(e){p.toast(apiErr(e,'导入失败'),'err')}
    gmiBusy.value=false;
  }
  async function withBusy(a,k,fn){busy.value={...busy.value,[a.id+'-'+k]:true};try{return await fn()}finally{const o={...busy.value};delete o[a.id+'-'+k];busy.value=o}}
  async function ref2(a){await withBusy(a,'refresh',async()=>{try{await api.post('/admin/accounts/'+a.id+'/refresh',{},p.token);p.toast('刷新成功');await load()}catch(e){p.toast(apiErr(e,'刷新失败'),'err')}})}
  async function saveMeta(a){await withBusy(a,'save',async()=>{try{const creditSnapshot=Math.max(0,Number(a._creditSnapshot)||0);const body={weight:parseInt(a._weight)||1,priority:parseInt(a._priority)||0};if(Number(a._creditSnapshot||0)!==Number(a._baseCreditSnapshot||0))body.credit_limit=creditSnapshot;await api.put('/admin/accounts/'+a.id,body,p.token);a._baseWeight=parseInt(a._weight)||1;a._basePriority=parseInt(a._priority)||0;a._baseCreditSnapshot=creditSnapshot;p.toast(body.credit_limit!==undefined?'已保存余额快照':'已保存');await load()}catch(e){p.toast(apiErr(e,'保存失败'),'err')}})}
  async function toggle(a){await withBusy(a,'toggle',async()=>{try{await api.put('/admin/accounts/'+a.id,{status:a.status==='active'?'inactive':'active'},p.token);p.toast(a.status==='active'?'已禁用':'已启用');await load()}catch(e){p.toast(apiErr(e,'操作失败'),'err')}})}
  async function testOne(a){tl.value=a.id;test.value=null;try{const r=await api.post('/admin/accounts/'+a.id+'/test',{model:'auto',prompt:'ping'},p.token);test.value={account:a.nickname||a.name,result:r};p.toast(r.ok?'测试成功':'测试失败',r.ok?'ok':'err');await load()}catch(e){p.toast(apiErr(e,'测试失败'),'err');test.value={account:a.nickname||a.name,result:{ok:false,status_code:0,message:e.message}}}tl.value=0}
  async function del(a){if(!confirm('删除账号 '+(a.nickname||a.name||a.id)+' ?'))return;await api.del('/admin/accounts/'+a.id,p.token);p.toast('已删除');await load();await discover(authPath.value)}
  function size(v){v=Number(v||0);if(v>=1024*1024)return(v/1024/1024).toFixed(1)+' MB';if(v>=1024)return(v/1024).toFixed(1)+' KB';return v+' B'}
  function credit(v){v=Number(v||0);return v.toLocaleString('zh-CN',{maximumFractionDigits:4})}
  function creditPct(a){return Math.max(0,Math.min(100,Number(a.credit_used_pct||0)))+'%'}
  function tokenLife(a){
    // api_key 类通道（gmi 等）无过期概念，不显示误导性的 "0h"。
    if(a.account_type==='api_key'||a.provider&&APIKEY_CHANNELS[a.provider])return '—';
    if(a.token_expired)return '过期';const h=Number(a.remaining_hours||0);if(h>=72)return '约 '+Math.floor(h/24)+' 天';if(h>=24)return Math.floor(h/24)+' 天 '+(h%24)+'h';return h+'h'
  }
  const visibleAccounts=computed(()=>{
    const q=filters.q.trim().toLowerCase();
    let rows=l.value.filter(a=>{
      const hay=[a.nickname,a.name,a.uid,a.domain,a.status].join(' ').toLowerCase();
      if(q&&!hay.includes(q))return false;
      if(filters.status!=='all'&&a.status!==filters.status)return false;
      return true;
    });
    rows=[...rows];
    rows.sort((a,b)=>{
      if(filters.sort==='used')return Number(b.total_credits||0)-Number(a.total_credits||0);
      if(filters.sort==='requests')return Number(b.total_requests||0)-Number(a.total_requests||0);
      return Number(b.priority||0)-Number(a.priority||0)||Number(b.weight||1)-Number(a.weight||1)||Number(a.total_requests||0)/Math.max(1,Number(a.weight||1))-Number(b.total_requests||0)/Math.max(1,Number(b.weight||1));
    });
    return rows;
  })
  function clearPath(){authPath.value='';discover('')}
  function stopSoloPoll(){if(soloTimer){clearTimeout(soloTimer);soloTimer=null}}
  async function startSoloLogin(){if(soloBusy.value)return;stopSoloPoll();soloGen++;const gen=soloGen;soloBusy.value=true;try{const r=await api.post('/admin/traesolo/login/start',{},p.token);if(gen!==soloGen)return;solo.pending=true;solo.url=r.login_url||'';solo.pendingId=r.pending_id||'';solo.callbackUrl=r.callback_url||'';solo.state='pending';solo.uid='';solo.error='';window.open(r.login_url,'_blank');pollSolo(gen)}catch(e){if(gen===soloGen){solo.pending=false;solo.error=''}p.toast(apiErr(e,'发起 SOLO 登录失败'),'err')}soloBusy.value=false}
  async function pollSolo(gen){if(gen!==soloGen)return;if(!solo.pendingId)return;try{const r=await api.get('/admin/traesolo/login/result?pending_id='+encodeURIComponent(solo.pendingId),p.token);if(gen!==soloGen)return;if(!r||r.found===false){stopSoloPoll();solo.pending=false;solo.state='expired';solo.error='登录会话已过期，可重新发起登录';return}solo.state=r.state||'';if(r.state==='success'){stopSoloPoll();solo.uid=r.uid||'';solo.pending=false;solo.error='';p.toast('SOLO 账号已添加'+(r.uid?'（'+r.uid+'）':''),'ok');await load();await discover();return}else if(r.state==='failed'){stopSoloPoll();solo.pending=false;solo.error=r.error||'登录失败';return}else if(r.state==='canceled'){stopSoloPoll();solo.pending=false;solo.error='';return}soloTimer=setTimeout(()=>pollSolo(gen),2500)}catch(e){if(gen!==soloGen)return;stopSoloPoll();solo.pending=false;if(e.message==='404'){solo.state='expired';solo.error='登录会话已过期，可重新发起登录'}else{solo.error='登录状态查询失败：'+apiErr(e);p.toast(solo.error,'err')}}}
  async function cancelSolo(){if(!solo.pendingId)return;stopSoloPoll();soloGen++;try{await api.post('/admin/traesolo/login/cancel',{pending_id:solo.pendingId},p.token);solo.pending=false;solo.state='canceled';solo.error=''}catch(e){}}
  async function completeSolo(){const u=solo.manual.trim();if(!u){p.toast('请先粘贴完整回调 URL','err');return}soloBusy.value=true;try{const r=await api.post('/admin/traesolo/login/complete',{callback:u},p.token);if(r.ok){p.toast('SOLO 账号已添加'+(r.uid?'（'+r.uid+'）':''),'ok');solo.manual='';await load();await discover()}else{p.toast(r.error||'导入失败','err')}}catch(e){p.toast(apiErr(e,'导入失败'),'err')}soloBusy.value=false}

  onMounted(()=>{loadChannels();load();if(!gmiMode.value)discover()});return{l,visibleAccounts,filters,ld,sa,ai,nm,disc,dl,scanning,adding,authPath,test,tl,busyKey,dirty,load,discover,scan,scanCustom,add,addApiKey,ref2,saveMeta,toggle,testOne,del,fmt,size,credit,tok,creditPct,tokenLife,clearPath,I,discChannel,channels,gmiMode,gmiKey,gmiNick,gmiBase,gmiBusy,APIKEY_PANEL,solo,soloBusy,startSoloLogin,cancelSolo,completeSolo}
},template:`
<div>
  <div class="phead"><h1>账号管理</h1><p>账号导入 · 调度权重与优先级 · 连通性测试（官方额度与积分领取在「额度与积分」页）</p></div>
  <div class="card">
    <div class="card-h">本机登录检测<span class="sub">只显示文件信息，不显示 token</span></div>
    <div class="card-p">
      <div class="detect-summary">
        <div>
          <div class="detect-title" v-if="gmiMode">{{APIKEY_PANEL.name}} · API Key 导入</div>
          <div class="detect-title" v-else-if="disc">检测到 {{disc.file_count}} 个登录文件，其中 {{disc.valid_count}} 个有效</div>
          <div class="detect-title" v-else>正在检测本机登录文件</div>
          <div class="hint" v-if="gmiMode">粘贴 API Key 即可入库。也支持环境变量 <span class="mono">{{APIKEY_PANEL.env}}</span>（无活跃账号时自动导入）。凭据不会回显，列表里只显示尾号。</div>
          <div class="hint" v-else>三个通道默认都能选。先选通道再检测导入；没登录的通道检测为空。启动默认不再自动入库。已导入账号默认只更新 token，不改权重/优先级。</div>
        </div>
        <div class="detect-actions" v-if="!gmiMode">
          <select class="selectctl" v-model="discChannel" @change="discover(authPath)"><option v-for="c in channels" :key="c.id" :value="c.id">{{c.display_name||c.id}}</option></select>
          <button class="btn s pri" @click="scan('')" :disabled="dl||scanning||!disc?.valid_count"><span v-html="I.scan"></span>{{scanning?'导入中':'一键导入本机登录'}}</button>
          <button class="btn s" @click="discover('')" :disabled="dl"><span v-html="I.refresh"></span>重新检测</button>
        </div>
        <div class="detect-actions" v-else>
          <select class="selectctl" v-model="discChannel"><option v-for="c in channels" :key="c.id" :value="c.id">{{c.display_name||c.id}}</option></select>
        </div>
      </div>
      <div v-if="gmiMode" class="notebox">
        <div class="field"><label>API Key（支持裸 Key / "Bearer xxx" / {"api_key":"..."} 三种粘贴形态）</label><textarea v-model="gmiKey" placeholder="粘贴 API Key" style="font-family:var(--mono)"></textarea></div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <div class="field" style="flex:1;min-width:180px"><label>昵称（可选）</label><input v-model="gmiNick" placeholder="如 main"/></div>
          <div class="field" style="flex:2;min-width:260px"><label>Base URL（可选，默认 {{APIKEY_PANEL.base}}）</label><input v-model="gmiBase" :placeholder="APIKEY_PANEL.base"/></div>
        </div>
        <div style="display:flex;gap:8px;align-items:center;margin-top:4px">
          <button class="btn s pri" @click="addApiKey" :disabled="gmiBusy">{{gmiBusy?'导入中':'导入 API Key'}}</button>
          <button class="btn s" @click="gmiKey='';gmiNick='';gmiBase=''">清空</button>
          <span class="hint" style="margin:0">导入后可在下方列表点「测试」验证连通性。</span>
        </div>
      </div>
      <template v-else>
      <div v-if="discChannel==='traesolo'" class="notebox">
        <h4 style="margin:0 0 8px">Web 登录（TRAE SOLO）</h4>
        <p class="hint" style="margin:0 0 10px">点击「发起网页登录」后在新窗口完成 TRAE 登录。本机部署时浏览器会自动跳回 <span class="mono">{{solo.callbackUrl||'/authorize'}}</span> 完成入库；远程部署够不到回调时，把浏览器地址栏里的完整回调 URL 粘到下方点「手动完成」。</p>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
          <button class="btn s pri" @click="startSoloLogin" :disabled="soloBusy||solo.pending">{{solo.pending?'等待登录…':(solo.state==='success'?'重新发起登录':'发起网页登录')}}</button>
          <button class="btn s" @click="cancelSolo" :disabled="!solo.pending">取消</button>
          <span class="tag" v-if="solo.state==='success'">成功 · {{solo.uid}}</span>
          <span class="tag" v-else-if="solo.state==='failed'">失败</span>
          <a v-if="solo.url" :href="solo.url" target="_blank" class="hint" style="word-break:break-all">重新打开登录 URL ↗</a>
        </div>
        <div class="callout" v-if="solo.error" style="margin:10px 0 0">{{solo.error}}</div>
        <div style="display:flex;gap:8px;margin-top:10px">
          <input v-model="solo.manual" placeholder="手动闭环：粘贴完整回调 URL（http://…/authorize?refreshToken=…）"/>
          <button class="btn s" @click="completeSolo" :disabled="soloBusy">{{soloBusy?'处理中':'手动完成'}}</button>
        </div>
      </div>
      <div class="callout" v-if="disc?.runtime?.host_auth_limited" style="margin-bottom:12px">当前是 Linux Docker。QClaw / 千问办公的 Windows 登录文件在容器里解不开。这两家请在本机用 <span class="mono">python server.py</span> 启动后再检测导入。WorkBuddy 可以继续用这个容器。</div>
      <div class="callout" v-else-if="disc?.runtime?.container && !disc.runtime.auth_mount_exists" style="margin-bottom:12px">当前运行在 Docker 容器内，容器不能直接扫描 Windows 的 C 盘。Windows Docker 推荐用 <span class="mono">.\start-docker-win.ps1</span> 启动，它会自动把默认登录目录只读挂载到 <span class="mono">/auth</span>。</div>
      <div v-if="dl" class="load" style="padding:18px"><div class="spin"></div></div>
      <template v-else>
        <div class="detect-grid">
          <div class="detect-box">
            <h4>默认扫描目录</h4>
            <div v-if="disc?.dirs?.length">
              <div class="detect-path" v-for="d in disc.dirs" :key="d.path">
                <span class="badge" :class="d.exists?(d.file_count?'ok':'inactive'):'err'">{{d.exists?d.file_count+' 个文件':'不存在'}}</span>
                <code :title="d.path">{{d.path}}</code>
              </div>
            </div>
            <div class="muted" v-else>暂无目录信息</div>
          </div>
          <div class="detect-box">
            <h4>自定义路径</h4>
            <div class="detect-custom">
              <input v-model="authPath" placeholder="auth 目录或 workbuddy-desktop.info 完整路径"/>
              <button class="btn s" @click="discover(authPath)" :disabled="dl">检测</button>
              <button class="btn s pri" @click="scanCustom" :disabled="dl||scanning">{{scanning?'导入中':'导入'}}</button>
              <button class="btn s" @click="clearPath" :disabled="dl">清空</button>
            </div>
            <div class="hint">可以直接粘贴 <span class="mono">C:/Users/.../auth</span> 或某个 <span class="mono">.info</span> 文件路径。</div>
          </div>
        </div>
        <div class="detect-files">
          <table class="mini-table" v-if="disc?.files?.length"><thead><tr><th>文件</th><th>状态</th><th>UID</th><th>域名</th><th>修改时间</th><th>大小</th><th>目录</th></tr></thead><tbody>
            <tr v-for="f in disc.files" :key="f.path"><td><div style="font-weight:600">{{f.account_name||f.name}}</div><div class="mono" :title="f.path">{{f.name}}</div></td><td><span class="badge" :class="f.valid?(f.already_imported?'inactive':'ok'):'err'">{{f.valid?(f.already_imported?'已导入':'可导入'):'无效'}}</span></td><td class="mono">{{f.uid_masked||'-'}}</td><td>{{f.domain||'-'}}</td><td class="mono">{{fmt(f.mtime)}}</td><td>{{size(f.size)}}</td><td class="mono" :title="f.dir">{{f.dir}}</td></tr>
          </tbody></table>
          <div class="empty" style="padding:18px" v-else>未检测到当前通道的登录文件</div>
        </div>
      </template>
      </template><!-- /file-login channels -->
    </div>
  </div>
  <div class="tbar"><button class="btn s" @click="load()" :disabled="ld"><span v-html="I.refresh"></span>{{ld?'刷新中':'刷新列表'}}</button><button class="btn s" @click="sa=true"><span v-html="I.plus"></span>高级手动添加</button><div class="spacer"></div><span class="tag" v-if="l.length">{{visibleAccounts.length}}/{{l.length}}个 · {{l.filter(a=>a.status==='active').length}}活跃</span></div>
  <div v-if="ld" class="load"><div class="spin"></div></div>
  <div class="card" v-else-if="l.length"><div class="card-h">账号列表<span class="sub">调度权重 / 优先级 / 余额快照 · 官方额度与每日积分在「额度与积分」页</span></div><div class="card-p" style="padding-bottom:0"><div class="control-row"><input class="searchbox" v-model="filters.q" placeholder="搜索账号 / UID / 域名"/><select class="selectctl" v-model="filters.status"><option value="all">全部状态</option><option value="active">active</option><option value="inactive">inactive</option><option value="expired">expired</option></select><select class="selectctl" v-model="filters.sort"><option value="priority">优先级 / 权重</option><option value="requests">请求数高到低</option><option value="used">累计已用高到低</option></select><div class="spacer"></div><span class="tag">当前 {{visibleAccounts.length}} 条</span></div></div><div class="table-scroll"><table><thead><tr><th>账号</th><th>通道</th><th>UID</th><th>状态</th><th>权重</th><th>优先级</th><th>余额快照</th><th>Token 有效期</th><th>请求</th><th>Token</th><th>累计已用</th><th></th></tr></thead><tbody>
    <tr v-for="a in visibleAccounts" :key="a.id"><td style="font-weight:600">{{a.nickname||a.name}} <span class="tag warn" v-if="dirty(a)">未保存</span></td><td><span class="tag">{{a.provider||'workbuddy'}}</span></td><td class="mono">{{a.uid?.slice(0,8)}}…</td><td><span class="badge" :class="a.status">{{a.status}}</span></td><td><input class="numctl" v-model.number="a._weight" type="number" min="1" max="100"/></td><td><input class="numctl" v-model.number="a._priority" type="number" min="-100" max="100"/></td><td class="credit-cell"><input class="numctl credit" v-model.number="a._creditSnapshot" type="number" min="0" step="0.01" placeholder="0"/><div class="credit-meta" v-if="a.credit_snapshot>0">余 {{credit(a.credit_remaining)}} · 已用 {{creditPct(a)}}</div><div class="credit-meta" v-else-if="a.total_credits>0">累计消耗(估算) {{credit(a.total_credits)}}</div><div class="credit-meta" v-else>官方失败时可手动校准</div></td><td>{{tokenLife(a)}}</td><td>{{a.total_requests}}</td><td>{{tok(a.total_tokens)}}</td><td>{{credit(a.total_credits)}}</td><td><div class="ops"><button class="btn s" @click="saveMeta(a)" :disabled="!dirty(a)||busyKey(a.id,'save')">{{busyKey(a.id,'save')?'保存中':'保存'}}</button><button class="btn s" @click="toggle(a)" :disabled="busyKey(a.id,'toggle')">{{busyKey(a.id,'toggle')?'处理中':(a.status==='active'?'禁用':'启用')}}</button><button class="btn s" @click="testOne(a)" :disabled="tl===a.id">{{tl===a.id?'测试中':'测试'}}</button><button class="btn s" @click="ref2(a)" :disabled="busyKey(a.id,'refresh')">{{busyKey(a.id,'refresh')?'刷新中':'刷新'}}</button><button class="btn s danger" @click="del(a)">删除</button></div></td></tr>
    <tr v-if="!visibleAccounts.length"><td colspan="12" class="empty">没有匹配的账号</td></tr>
  </tbody></table></div></div>
  <div class="card card-p empty" v-else><div class="em">🔌</div><p>暂无账号 · 从上方导入：登录文件类通道用「本机登录检测」，API Key 类通道（如 GMI Cloud）选中后直接粘贴导入</p></div>
  <div class="ov" v-if="test" @click.self="test=null"><div class="modal"><div class="modal-h"><h3>账号测试 · {{test.account}}</h3><button class="x" @click="test=null">&times;</button></div><div class="modal-b"><div class="testbox"><div class="row"><span>状态</span><span><span class="badge" :class="test.result.ok?'ok':'err'">{{test.result.ok?'成功':'失败'}}</span></span></div><div class="row"><span>HTTP</span><span class="mono">{{test.result.status_code}}</span></div><div class="row"><span>耗时</span><span class="mono">{{test.result.duration_ms}}ms</span></div><div class="row" v-if="test.result.model"><span>模型</span><span class="mono">{{test.result.model}}</span></div><div class="row" v-if="test.result.usage"><span>Token</span><span class="mono">{{tok(test.result.usage.total_tokens)}}</span></div><div class="msg">{{test.result.message||'无返回内容'}}</div></div><div class="hint" style="margin-top:8px">测试会发送一次极短请求，并记录到请求日志。</div></div><div class="modal-f"><button class="btn pri" @click="test=null">关闭</button></div></div></div>
  <div class="ov" v-if="sa" @click.self="sa=false"><div class="modal"><div class="modal-h"><h3>高级手动添加</h3><button class="x" @click="sa=false">&times;</button></div><div class="modal-b"><div class="field"><label>名称</label><input v-model="nm" placeholder="可选"/></div><div class="field"><label>Auth JSON 或 API Key</label><textarea v-model="ai" placeholder="粘贴 .info 文件内容；单 key 通道可直接粘 API Key / Bearer xxx"></textarea><div class="hint">WorkBuddy 等登录文件通道粘完整 JSON；GMI 等单 key 通道可直接粘 API Key（自动识别）。</div></div></div><div class="modal-f"><button class="btn" @click="sa=false" :disabled="adding">取消</button><button class="btn pri" @click="add" :disabled="adding">{{adding?'添加中':'添加'}}</button></div></div></div>
</div>`};
