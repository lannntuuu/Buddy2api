import {api,apiErr,fmt,tok} from '../api.js';
import {I} from '../icons.js';
const{ref,reactive,computed,onMounted}=Vue;

export default {props:['token','toast'],setup(p){
  // ────────── master list ──────────
  const list=ref([]),ld=ref(true),err=ref(''),envLocked=ref(false),activeChannel=ref(''),toggling=ref({});
  async function loadList(){
    ld.value=true;err.value='';
    try{
      const r=await api.get('/admin/channels',p.token);
      list.value=(r.channels||[]).map(c=>({...c}));
      envLocked.value=!!r.env_locked;
      if(!list.value.some(c=>c.id===activeChannel.value))activeChannel.value=list.value.find(c=>c.enabled)?.id||list.value[0]?.id||'';
    }catch(e){err.value=apiErr(e,'加载通道列表失败')}
    ld.value=false;
  }
  function toggleChannel(id, on){
    if(envLocked.value){p.toast('CD env锁定中，无法更改（CB_GATEWAY_PROVIDERS）','err');return}
    const c=list.value.find(x=>x.id===id);if(c&&c.id!=='workbuddy')c.enabled=on;
    toggling.value={...toggling.value,[id]:true};
    api.put('/admin/channels',{enabled:list.value.filter(x=>x.enabled).map(x=>x.id),order:list.value.map(x=>x.id)},p.token).then(r=>{
      list.value.forEach(c=>{c.enabled=(r.enabled||[]).includes(c.id)});
    }).catch(e=>{p.toast(apiErr(e,'保存失败'),'err');loadList();}).finally(()=>{const o={...toggling.value};delete o[id];toggling.value=o});
  }
  const activeCh=computed(()=>list.value.find(c=>c.id===activeChannel.value));

  // ────────── definition: custom channel CRUD ──────────
  const ccList=ref([]),ccLd=ref(false),ccBusy=ref(false),ccErr=ref('');
  const ccForm=ref({mode:'create',draft:emptyCcDraft(),warning:null});
  function emptyCcDraft(){return {id:'',display_name:'',base_url:'',modelsText:'',aliases:'',env_api_key:'',api_key:''}}
  async function loadCC(){
    ccLd.value=true;ccErr.value='';
    try{const r=await api.get('/admin/channels/custom',p.token);ccList.value=r.channels||[]}
    catch(e){ccErr.value=apiErr(e,'加载自定义通道失败')}
    ccLd.value=false;
  }
  function ccOf(id){return ccList.value.find(x=>x.id===id)}
  function ccStartCreate(){ccForm.value={mode:'create',draft:emptyCcDraft(),warning:null}}
  function ccStartEdit(c){
    ccForm.value={mode:'edit',warning:null,draft:{
      id:c.id,display_name:c.display_name||'',base_url:c.base_url||'',
      modelsText:(c.models||[]).join(', '),
      aliases:Object.entries(c.aliases||{}).map(([k,v])=>k+'→'+v).join('\n'),
      env_api_key:c.env_api_key||'',api_key:''
    }};
  }
  function ccCancel(){ccForm.value={mode:'create',draft:emptyCcDraft(),warning:null}}
  async function ccSave(){
    if(ccBusy.value)return;
    const f=ccForm.value.draft;
    if(!f.id.trim()||!f.display_name.trim()||!f.base_url.trim()){p.toast('id / 名称 / Base URL 必填','err');return}
    const models=f.modelsText.split(',').map(s=>s.trim()).filter(Boolean);
    if(!models.length){p.toast('至少填一个模型 id（逗号分隔）','err');return}
    const aliases={};f.aliases.split(/\r?\n/).forEach(line=>{const [k,...rest]=line.split('→');const v=rest.join('→');const ak=(k||'').trim(),av=(v||'').trim();if(ak&&av)aliases[ak]=av});
    const body={display_name:f.display_name.trim(),base_url:f.base_url.trim(),models,aliases};
    if(f.env_api_key.trim())body.env_api_key=f.env_api_key.trim();else body.env_api_key='';
    if(f.api_key.trim())body.api_key=f.api_key.trim();
    ccBusy.value=true;
    try{
      if(ccForm.value.mode==='create'){
        body.id=f.id.trim();
        const r=await api.post('/admin/channels/custom',body,p.token);
        if(r&&r.status==='saved_with_warning'&&r.warning){
          ccForm.value.warning=r.warning;p.toast('已保存，但探活失败：HTTP '+r.warning.probe_status,'err');
        }else{p.toast('已添加 '+body.id)}
        ccCancel();
      }else{
        if(!f.api_key.trim())delete body.api_key;
        const r=await api.put('/admin/channels/custom/'+encodeURIComponent(f.id),body,p.token);
        if(r&&r.status==='saved_with_warning'&&r.warning){
          ccForm.value.warning=r.warning;p.toast('已保存，但探活失败：HTTP '+r.warning.probe_status,'err');
        }else{p.toast('已更新 '+f.id)}
        ccCancel();
      }
      await Promise.all([loadCC(),loadList()]);
    }catch(e){p.toast(apiErr(e,'保存失败'),'err')}
    ccBusy.value=false;
  }
  async function ccDelete(c){
    if(!confirm('删除自定义通道 '+c.id+' ？该通道账号行将全部置 inactive。'))return;
    try{
      await api.del('/admin/channels/custom/'+encodeURIComponent(c.id),p.token);
      p.toast('已删除 '+c.id);
      await Promise.all([loadCC(),loadList()]);
    }catch(e){
      const m=String(e.message||'');
      p.toast(m==='409'?'seed 通道不允许删除，请用「启用通道」开关停用':'删除失败：'+apiErr(e),'err');
    }
  }

  // ────────── credential section: KEY_PANEL meta + discover/scan/solo ──────────
  // 合并自 accounts.js: KEY_PANEL_META = id → {name, base, env}，从 /admin/channels + /admin/channels/custom 推导
  const KEY_PANEL_META=ref({});
  function refreshKeyMeta(){
    const SEED_DEFAULT={gmi:{base:'https://api.gmi-serving.com/v1',env:'CB_GMI_API_KEY'},bailian:{base:'https://llm-7dqe434wikmhz0wa.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',env:'CB_BAILIAN_API_KEY'}};
    const meta={};
    for(const c of list.value){
      if(c.kind!=='apikey')continue;
      const seed=SEED_DEFAULT[c.id]||{};
      const custom=ccList.value.find(x=>x.id===c.id);
      meta[c.id]={name:c.display_name||c.id,base:(custom&&custom.base_url)||seed.base||'',env:(custom&&custom.env_api_key)||seed.env||''};
    }
    KEY_PANEL_META.value=meta;
  }
  const keyKey=ref(''),keyNick=ref(''),keyBase=ref(''),keyBusy=ref(false);
  const keyMode=computed(()=>{const c=activeCh.value;return c&&c.kind==='apikey'?c.id:''});
  const KEY_PANEL=computed(()=>KEY_PANEL_META.value[keyMode.value]||{name:'',base:'',env:''});

  const disc=ref(null),dl=ref(false),scanning=ref(false),authPath=ref('');
  const solo=reactive({pending:false,url:'',pendingId:'',callbackUrl:'',state:'',uid:'',error:'',manual:''}),soloBusy=ref(false);let soloTimer=null,soloGen=0;
  const soloSelected=computed(()=>activeChannel.value==='traesolo');
  async function discover(path=''){
    if(!activeChannel.value){p.toast('请先选中一个通道','err');return}
    dl.value=true;
    try{
      const qs=new URLSearchParams();
      if(path&&path.trim())qs.set('auth_dir',path.trim());
      if(activeChannel.value)qs.set('channel',activeChannel.value);
      const q=qs.toString();
      disc.value=await api.get('/admin/accounts/discover'+(q?'?'+q:''),p.token);
    }catch(e){p.toast(apiErr(e,'检测失败'),'err')}
    dl.value=false;
  }
  async function scan(path=''){
    if(scanning.value)return;
    scanning.value=true;
    try{
      if(disc.value?.preview_token){
        const body={channel:disc.value.channel||'workbuddy',preview_token:disc.value.preview_token};
        if(path&&path.trim())body.auth_dir=path.trim();
        const r=await api.post('/admin/accounts/import',body,p.token);
        p.toast('导入 '+r.imported+' · 更新 '+r.updated+' · 跳过 '+r.skipped);
      }else{
        const body=path&&path.trim()?{auth_dir:path.trim()}:{};
        const r=await api.post('/admin/accounts/scan',body,p.token);
        p.toast('导入 '+r.imported+' · 更新 '+r.updated+' · 跳过 '+r.skipped);
      }
      await loadAccounts();await discover(path);
    }catch(e){p.toast(apiErr(e,'扫描失败'),'err')}
    scanning.value=false;
  }
  async function scanCustom(){const path=authPath.value.trim();if(!path){p.toast('请先填写目录或 .info 文件路径','err');return}await scan(path)}
  function clearPath(){authPath.value='';discover('')}
  async function addApiKey(){
    const ch=KEY_PANEL.value;if(!keyMode.value||keyBusy.value)return;
    if(!keyKey.value.trim()){p.toast('请先粘贴 '+(ch.name||keyMode.value)+' API Key','err');return}
    keyBusy.value=true;
    try{
      const body={provider:keyMode.value,api_key:keyKey.value.trim()};
      if(keyNick.value.trim())body.nickname=keyNick.value.trim();
      if(keyBase.value.trim())body.base_url=keyBase.value.trim();
      const r=await api.post('/admin/accounts',body,p.token);
      p.toast((r.updated?'Key 已更新 · ':'导入成功 · ')+(r.uid||''),'ok');
      keyKey.value='';keyNick.value='';keyBase.value='';
      await loadAccounts();
    }catch(e){p.toast(apiErr(e,'导入失败'),'err')}
    keyBusy.value=false;
  }
  function stopSoloPoll(){if(soloTimer){clearTimeout(soloTimer);soloTimer=null}}
  async function startSoloLogin(){
    if(soloBusy.value)return;
    stopSoloPoll();soloGen++;
    const gen=soloGen;soloBusy.value=true;
    try{
      const r=await api.post('/admin/traesolo/login/start',{},p.token);
      if(gen!==soloGen)return;
      solo.pending=true;solo.url=r.login_url||'';solo.pendingId=r.pending_id||'';
      solo.callbackUrl=r.callback_url||'';solo.state='pending';solo.uid='';solo.error='';
      window.open(r.login_url,'_blank');pollSolo(gen);
    }catch(e){if(gen===soloGen){solo.pending=false;solo.error=''}p.toast(apiErr(e,'发起 SOLO 登录失败'),'err')}
    soloBusy.value=false;
  }
  async function pollSolo(gen){
    if(gen!==soloGen)return;if(!solo.pendingId)return;
    try{
      const r=await api.get('/admin/traesolo/login/result?pending_id='+encodeURIComponent(solo.pendingId),p.token);
      if(gen!==soloGen)return;
      if(!r||r.found===false){stopSoloPoll();solo.pending=false;solo.state='expired';solo.error='登录会话已过期，可重新发起登录';return}
      solo.state=r.state||'';
      if(r.state==='success'){stopSoloPoll();solo.uid=r.uid||'';solo.pending=false;solo.error='';p.toast('SOLO 账号已添加'+(r.uid?'（'+r.uid+'）':''),'ok');await loadAccounts();await discover();return}
      else if(r.state==='failed'){stopSoloPoll();solo.pending=false;solo.error=r.error||'登录失败';return}
      else if(r.state==='canceled'){stopSoloPoll();solo.pending=false;solo.error='';return}
      soloTimer=setTimeout(()=>pollSolo(gen),2500);
    }catch(e){if(gen!==soloGen)return;stopSoloPoll();solo.pending=false;if(e.message==='404'){solo.state='expired';solo.error='登录会话已过期，可重新发起登录'}else{solo.error='登录状态查询失败：'+apiErr(e);p.toast(solo.error,'err')}}
  }
  async function cancelSolo(){if(!solo.pendingId)return;stopSoloPoll();soloGen++;try{await api.post('/admin/traesolo/login/cancel',{pending_id:solo.pendingId},p.token);solo.pending=false;solo.state='canceled';solo.error=''}catch(e){}}
  async function completeSolo(){
    const u=solo.manual.trim();if(!u){p.toast('请先粘贴完整回调 URL','err');return}
    soloBusy.value=true;
    try{
      const r=await api.post('/admin/traesolo/login/complete',{callback:u},p.token);
      if(r.ok){p.toast('SOLO 账号已添加'+(r.uid?'（'+r.uid+'）':''),'ok');solo.manual='';await loadAccounts();await discover()}
      else{p.toast(r.error||'导入失败','err')}
    }catch(e){p.toast(apiErr(e,'导入失败'),'err')}
    soloBusy.value=false;
  }

  // ────────── accounts list (full table from accounts.js) ──────────
  const accs=ref([]),accLd=ref(false),accBusy=ref({}),test=ref(null),tl=ref(0),filters=reactive({q:'',status:'all',sort:'priority',provider:'all'});
  function hydrate(a){return {...a,_weight:a.weight||1,_priority:a.priority||0,_creditSnapshot:a.credit_snapshot||a.credit_limit||0,_baseWeight:a.weight||1,_basePriority:a.priority||0,_baseCreditSnapshot:a.credit_snapshot||a.credit_limit||0}}
  function dirty(a){return Number(a._weight||1)!==Number(a._baseWeight||1)||Number(a._priority||0)!==Number(a._basePriority||0)||Number(a._creditSnapshot||0)!==Number(a._baseCreditSnapshot||0)}
  function busyKey(id,k){return accBusy.value[id+'-'+k]}
  async function loadAccounts(){
    accLd.value=true;
    try{accs.value=(await api.get('/admin/accounts',p.token)).map(hydrate)}
    catch(e){p.toast(apiErr(e),'err')}
    accLd.value=false;
  }
  function size(v){v=Number(v||0);if(v>=1024*1024)return(v/1024/1024).toFixed(1)+' MB';if(v>=1024)return(v/1024).toFixed(1)+' KB';return v+' B'}
  function credit(v){v=Number(v||0);return v.toLocaleString('zh-CN',{maximumFractionDigits:4})}
  function creditPct(a){return Math.max(0,Math.min(100,Number(a.credit_used_pct||0)))+'%'}
  function tokenLife(a){
    if(a.account_type==='api_key'||a.provider&&KEY_PANEL_META.value[a.provider])return '-';
    if(a.token_expired)return '过期';
    const h=Number(a.remaining_hours||0);if(h>=72)return '约 '+Math.floor(h/24)+' 天';if(h>=24)return Math.floor(h/24)+' 天 '+(h%24)+'h';return h+'h'
  }
  const visibleAccounts=computed(()=>{
    const q=filters.q.trim().toLowerCase();
    let rows=accs.value.filter(a=>{
      const hay=[a.nickname,a.name,a.uid,a.domain,a.status].join(' ').toLowerCase();
      if(q&&!hay.includes(q))return false;
      if(filters.status!=='all'&&a.status!==filters.status)return false;
      if(filters.provider!=='all'&&a.provider!==filters.provider)return false;
      return true;
    });
    rows=[...rows];
    rows.sort((a,b)=>{
      if(filters.sort==='used')return Number(b.total_credits||0)-Number(a.total_credits||0);
      if(filters.sort==='requests')return Number(b.total_requests||0)-Number(a.total_requests||0);
      return Number(b.priority||0)-Number(a.priority||0)||Number(b.weight||1)-Number(a.weight||1)||Number(a.total_requests||0)/Math.max(1,Number(a.weight||1))-Number(b.total_requests||0)/Math.max(1,Number(b.weight||1));
    });
    return rows;
  });
  async function withBusy(a,k,fn){accBusy.value={...accBusy.value,[a.id+'-'+k]:true};try{return await fn()}finally{const o={...accBusy.value};delete o[a.id+'-'+k];accBusy.value=o}}
  async function ref2(a){await withBusy(a,'refresh',async()=>{try{await api.post('/admin/accounts/'+a.id+'/refresh',{},p.token);p.toast('刷新成功');await loadAccounts()}catch(e){p.toast(apiErr(e,'刷新失败'),'err')}})}
  async function saveMeta(a){await withBusy(a,'save',async()=>{try{const creditSnapshot=Math.max(0,Number(a._creditSnapshot)||0);const body={weight:parseInt(a._weight)||1,priority:parseInt(a._priority)||0};if(Number(a._creditSnapshot||0)!==Number(a._baseCreditSnapshot||0))body.credit_limit=creditSnapshot;await api.put('/admin/accounts/'+a.id,body,p.token);a._baseWeight=parseInt(a._weight)||1;a._basePriority=parseInt(a._priority)||0;a._baseCreditSnapshot=creditSnapshot;p.toast(body.credit_limit!==undefined?'已保存余额快照':'已保存');await loadAccounts()}catch(e){p.toast(apiErr(e,'保存失败'),'err')}})}
  async function toggle(a){await withBusy(a,'toggle',async()=>{try{await api.put('/admin/accounts/'+a.id,{status:a.status==='active'?'inactive':'active'},p.token);p.toast(a.status==='active'?'已禁用':'已启用');await loadAccounts()}catch(e){p.toast(apiErr(e,'操作失败'),'err')}})}
  async function testOne(a){tl.value=a.id;test.value=null;try{const r=await api.post('/admin/accounts/'+a.id+'/test',{model:'auto',prompt:'ping'},p.token);test.value={account:a.nickname||a.name,result:r};p.toast(r.ok?'测试成功':'测试失败',r.ok?'ok':'err');await loadAccounts()}catch(e){p.toast(apiErr(e,'测试失败'),'err');test.value={account:a.nickname||a.name,result:{ok:false,status_code:0,message:e.message}}}tl.value=0}
  async function del(a){if(!confirm('删除账号 '+(a.nickname||a.name||a.id)+' ?'))return;await api.del('/admin/accounts/'+a.id,p.token);p.toast('已删除');await loadAccounts();await discover(authPath.value)}

  // ────────── 高级手动添加 (modal) ──────────
  const sa=ref(false),ai=ref(''),nm=ref(''),adding=ref(false);
  async function add(){
    if(adding.value)return;
    if(!ai.value.trim()){p.toast('请先粘贴 Auth JSON','err');return}
    let d;
    try{d=JSON.parse(ai.value)}catch(e){
      const raw=ai.value.trim();
      if(raw&&raw.length<4096&&!raw.startsWith('{')&&!raw.startsWith('['))d={api_key:raw};
      else{p.toast('失败:'+e.message,'err');adding.value=false;return}
    }
    adding.value=true;
    try{
      const r=await api.post('/admin/accounts',{...d,name:nm.value,provider:d.provider||d.channel||activeChannel.value},p.token);
      p.toast(r.updated?'账号已更新':'添加成功');sa.value=false;ai.value='';nm.value='';await loadAccounts();await discover(authPath.value);
    }catch(e){p.toast('失败:'+e.message,'err')}
    adding.value=false;
  }

  onMounted(async()=>{
    await Promise.all([loadList(),loadCC(),loadAccounts()]);
    refreshKeyMeta();
    // 选中后默认做一次本机检测（登录型通道；密钥型自带面板）
    if(activeChannel.value&&activeChannel.value!=='traesolo')await discover('');
  });
  watch(activeChannel,async(v,old)=>{
    if(!v)return;
    if(v===old)return;
    refreshKeyMeta();
    // 切通道时清空粘的 key 和 solo 状态
    keyKey.value='';keyNick.value='';keyBase.value='';
    solo.pending=false;solo.url='';solo.pendingId='';solo.state='';solo.error='';solo.manual='';solo.uid='';
    stopSoloPoll();
    // 选中后默认做一次本机检测（登录型）
    if(v!=='traesolo')await discover('');
  });
  watch(list,()=>refreshKeyMeta(),{deep:false});
  watch(ccList,()=>refreshKeyMeta(),{deep:false});

  return{list,ld,err,envLocked,activeChannel,toggling,loadList,toggleChannel,activeCh,ccList,ccLd,ccBusy,ccErr,ccForm,ccOf,ccStartCreate,ccStartEdit,ccCancel,ccSave,ccDelete,KEY_PANEL,keyMode,keyKey,keyNick,keyBase,keyBusy,addApiKey,disc,dl,scanning,authPath,discover,scan,scanCustom,clearPath,solo,soloBusy,soloSelected,startSoloLogin,cancelSolo,completeSolo,accs,accLd,visibleAccounts,filters,busyKey,dirty,ref2,saveMeta,toggle,testOne,del,loadAccounts,size,credit,creditPct,tokenLife,test,tl,sa,ai,nm,adding,add,fmt,tok,I}
},template:`
<div>
  <div class="phead"><h1>通道管理</h1><p>定义通道 · 管理凭证 · 启用开关</p></div>

  <div class="card">
    <div class="card-h">通道列表<span class="sub">点行选中查看详情 · 开关即时生效</span></div>
    <div v-if="ld" class="load"><div class="spin"></div></div>
    <div v-else-if="err" style="padding:16px;color:var(--err);font-size:12px">{{err}}</div>
    <div v-else class="card-p" style="padding:0">
      <div v-if="envLocked" class="status-line ch-warn" style="margin:0;padding:10px 16px;border-bottom:1px solid var(--border)">
        检测到环境变量 <code>CB_GATEWAY_PROVIDERS</code>，通道开关为只读；如需在 UI 内调整，请去掉环境变量后重启。
      </div>
      <div class="table-scroll"><table>
        <thead><tr><th>显示名</th><th>ID</th><th>徽标</th><th style="width:120px;text-align:right">启用</th></tr></thead>
        <tbody>
          <tr v-for="c in list" :key="c.id" @click="activeChannel=c.id" :class="{on:activeChannel===c.id}" style="cursor:pointer">
            <td>{{c.display_name||c.id}}</td>
            <td class="mono">{{c.id}}</td>
            <td>
              <span class="tag" :class="c.kind==='apikey'?'apikey':'builtin'">{{c.kind||'builtin'}}</span>
              <span v-if="c.custom" class="tag" style="margin-left:4px;background:var(--accent-soft);color:var(--accent)">custom</span>
              <span v-else-if="c.id==='workbuddy'" class="tag" style="margin-left:4px">必选</span>
              <span v-else-if="!c.loaded" class="tag warn" style="margin-left:4px">未加载</span>
            </td>
            <td style="text-align:right"><input type="checkbox" :checked="c.enabled" :disabled="envLocked||c.id==='workbuddy'||toggling[c.id]" @change="toggleChannel(c.id,$event.target.checked)" @click.stop/></td>
          </tr>
        </tbody>
      </table></div>
    </div>
  </div>

  <div v-if="activeCh" class="card" style="margin-top:16px">
    <div class="card-h">详情 · < strong style="font-family:var(--mono)">{{activeCh.id}}</strong><span class="sub" style="margin-left:8px">{{activeCh.display_name||activeCh.id}}</span></div>
    <div class="card-p">

      <!-- ── definition section ── -->
      <div class="sec-h">定义</div>
      <div v-if="ccOf(activeCh.id)" style="border:1px solid var(--border);border-radius:6px;padding:12px;background:var(--bg-elevated)">
        <div v-if="ccForm.mode==='edit'&&ccForm.draft.id===activeCh.id">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px"><strong style="font-family:var(--mono)">编辑 {{ccForm.draft.id}}</strong><span class="tag" v-if="ccOf(activeCh.id).source==='seed'">seed</span></div>
          <div class="form-grid">
            <div class="field"><label>通道 ID</label><input :value="ccForm.draft.id" disabled/></div>
            <div class="field"><label>显示名称 *</label><input v-model="ccForm.draft.display_name" placeholder="如 内部代理"/></div>
            <div class="field"><label>Base URL *<span class="hint" style="margin:0">https:// 或 http://127.0.0.1[:port]/localhost[:port]</span></label><input v-model="ccForm.draft.base_url"/></div>
            <div class="field"><label>模型白名单 *<span class="hint" style="margin:0">逗号分隔</span></label><input v-model="ccForm.draft.modelsText" placeholder="model-a, model-b"/></div>
            <div class="field"><label>别名<span class="hint" style="margin:0">每行 "别名→模型 id"</span></label><textarea v-model="ccForm.draft.aliases" rows="3" style="font-family:var(--mono)"></textarea></div>
            <div class="field"><label>环境变量名<span class="hint" style="margin:0">匹配 ^CB_[A-Z0-9_]+$</span></label><input v-model="ccForm.draft.env_api_key" placeholder="CB_MY_KEY"/></div>
            <div class="field"><label>API Key（轮换）<span class="hint" style="margin:0">留空保留旧 Key；填则轮换</span></label><input v-model="ccForm.draft.api_key" type="password" placeholder="留空不轮换"/></div>
          </div>
          <div v-if="ccForm.warning" class="callout" style="margin-top:10px;font-size:12px;background:var(--warn-bg);border-color:var(--warn-border);color:var(--warn-fg)">探活失败（HTTP {{ccForm.warning.probe_status}}）：{{ccForm.warning.probe_error||'无返回内容'}}。已保存定义，可后续调整 Base URL 重试。</div>
          <div style="display:flex;gap:8px;margin-top:12px"><button class="btn s pri" @click="ccSave" :disabled="ccBusy">{{ccBusy?'保存中…':'保存'}}</button><button class="btn s" @click="ccCancel" :disabled="ccBusy">取消</button></div>
        </div>
        <div v-else>
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
            <div>
              <div><span class="hint" style="margin:0">显示名</span> <strong>{{ccOf(activeCh.id).display_name}}</strong></div>
              <div><span class="hint" style="margin:0">Base URL</span> <code class="mono">{{ccOf(activeCh.id).base_url}}</code></div>
              <div><span class="hint" style="margin:0">模型</span> <span class="mono">{{(ccOf(activeCh.id).models||[]).join(', ')}}</span></div>
              <div><span class="hint" style="margin:0">别名</span> <span class="mono">{{Object.entries(ccOf(activeCh.id).aliases||{}).map(([k,v])=>k+'→'+v).join(', ')||'无'}}</span></div>
              <div><span class="hint" style="margin:0">环境变量</span> <span class="mono">{{ccOf(activeCh.id).env_api_key||'-'}}</span></div>
              <div v-if="ccOf(activeCh.id).source"><span class="hint" style="margin:0">来源</span> <span class="tag">{{ccOf(activeCh.id).source}}</span></div>
            </div>
            <div style="display:flex;gap:6px;flex-shrink:0">
              <button class="btn s" @click="ccStartEdit(ccOf(activeCh.id))"><span v-html="I.plus"></span>编辑</button>
              <button class="btn s danger" @click="ccDelete(ccOf(activeCh.id))" v-if="ccOf(activeCh.id).source!=='seed'" title="删除自定义通道">删除</button>
              <button class="btn s danger" v-else disabled title="seed 通道不允许删除，请用「启用通道」开关停用">删除(禁用)</button>
            </div>
          </div>
        </div>
      </div>
      <div v-else style="border:1px solid var(--border);border-radius:6px;padding:12px;background:var(--bg-elevated)">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
          <div>
            <div><span class="hint" style="margin:0">类型</span> <strong>{{activeCh.kind==='apikey'?'密钥型':'登录型'}}</strong></div>
            <div v-if="KEY_PANEL_META[activeCh.id]"><span class="hint" style="margin:0">Base URL</span> <code class="mono">{{KEY_PANEL_META[activeCh.id].base||'内置默认'}}</code></div>
            <div v-if="KEY_PANEL_META[activeCh.id]"><span class="hint" style="margin:0">环境变量</span> <span class="mono">{{KEY_PANEL_META[activeCh.id].env||'-'}}</span></div>
            <div v-if="activeCh.checkin_supported"><span class="hint" style="margin:0">签到</span> <span class="tag">支持</span></div>
          </div>
          <div class="hint" style="margin:0">内置通道 · 定义不可编辑；启用 / 停用在上方开关</div>
        </div>
      </div>

      <!-- ── credential section ── -->
      <div class="sec-h" style="margin-top:18px">凭证</div>

      <!-- key (apikey) -->
      <div v-if="keyMode" class="notebox">
        <div class="detect-title">{{KEY_PANEL.name}} · 上游密钥配置</div>
        <div class="hint">粘贴该平台的上游 API Key 即可入库。也支持环境变量 <span class="mono">{{KEY_PANEL.env}}</span>（无可用密钥时自动导入）。密钥不会回显，列表里只显示尾号。注意：这是上游通行证，与「API Keys」页发给客户端的网关 Key 是两回事。</div>
        <div class="field"><label>上游密钥（支持裸 Key / "Bearer xxx" / {"api_key":"..."} 三种粘贴形态）</label><textarea v-model="keyKey" placeholder="粘贴上游 API Key" style="font-family:var(--mono)"></textarea></div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <div class="field" style="flex:1;min-width:180px"><label>昵称（可选）</label><input v-model="keyNick" placeholder="如 main"/></div>
          <div class="field" style="flex:2;min-width:260px"><label>Base URL（可选，默认 {{KEY_PANEL.base||'由通道定义决定'}}）</label><input v-model="keyBase" :placeholder="KEY_PANEL.base"/></div>
        </div>
        <div style="display:flex;gap:8px;align-items:center;margin-top:4px">
          <button class="btn s pri" @click="addApiKey" :disabled="keyBusy">{{keyBusy?'导入中':'保存密钥'}}</button>
          <button class="btn s" @click="keyKey='';keyNick='';keyBase=''">清空</button>
          <span class="hint" style="margin:0">导入后可在下方列表点「测试」验证连通性。</span>
        </div>
      </div>

      <!-- login-type: solo special -->
      <div v-else-if="soloSelected" class="notebox">
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

      <!-- login-type: discover + scan -->
      <div v-else class="notebox">
        <div class="detect-title" v-if="disc">检测到 {{disc.file_count}} 个登录文件，其中 {{disc.valid_count}} 个有效</div>
        <div class="detect-title" v-else-if="dl">正在检测本机登录文件</div>
        <div class="detect-title" v-else>本机登录检测（先选中通道再检测导入；没登录的通道检测为空）</div>
        <div class="hint">启动默认不再自动入库。已导入账号默认只更新 token，不改权重/优先级。</div>
        <div class="callout" v-if="disc?.runtime?.host_auth_limited" style="margin-bottom:12px">当前是 Linux Docker。QClaw / 千问办公的 Windows 登录文件在容器里解不开。这两家请在本机用 <span class="mono">python server.py</span> 启动后再检测导入。WorkBuddy 可以继续用这个容器。</div>
        <div class="callout" v-else-if="disc?.runtime?.container && !disc.runtime.auth_mount_exists" style="margin-bottom:12px">当前运行在 Docker 容器内，容器不能直接扫描 Windows 的 C 盘。Windows Docker 推荐用 <span class="mono">.\start-docker-win.ps1</span> 启动，它会自动把默认登录目录只读挂载到 <span class="mono">/auth</span>。</div>
        <div v-if="dl" class="load" style="padding:18px"><div class="sp-spin"></div></div>
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
      </div>

      <!-- ── accounts list ── -->
      <div class="sec-h" style="margin-top:18px">凭证列表</div>
      <div class="tbar"><button class="btn s" @click="loadAccounts" :disabled="accLd"><span v-html="I.refresh"></span>{{accLd?'刷新中':'刷新列表'}}</button><button class="btn s" @click="sa=true"><span v-html="I.plus"></span>高级手动添加</button><div class="spacer"></div><span class="tag" v-if="accs.length">{{visibleAccounts.length}}/{{accs.length}}个 · {{accs.filter(a=>a.status==='active').length}}活跃</span></div>
      <div v-if="accLd" class="load"><div class="spin"></div></div>
      <div class="card" v-else-if="accs.length"><div class="card-p" style="padding-bottom:0"><div class="control-row"><input class="searchbox" v-model="filters.q" placeholder="搜索账号 / UID / 域名"/><select class="selectctl" v-model="filters.status"><option value="all">全部状态</option><option value="active">active</option><option value="inactive">inactive</option><option value="expired">expired</option></select><select class="selectctl" v-model="filters.provider"><option value="all">全部通道</option><option v-for="c in list" :key="'fp-'+c.id" :value="c.id">{{c.display_name||c.id}}</option></select><select class="selectctl" v-model="filters.sort"><option value="priority">优先级 / 权重</option><option value="requests">请求数高到低</option><option value="used">累计已用高到低</option></select><div class="spacer"></div><span class="tag">当前 {{visibleAccounts.length}} 条</span></div></div><div class="table-scroll"><table><thead><tr><th>账号</th><th>通道</th><th>UID</th><th>状态</th><th>权重</th><th>优先级</th><th>余额快照</th><th>Token 有效期</th><th>请求</th><th>Token</th><th>累计已用</th><th></th></tr></thead><tbody>
        <tr v-for="a in visibleAccounts" :key="a.id"><td style="font-weight:600">{{a.nickname||a.name}} <span class="tag warn" v-if="dirty(a)">未保存</span></td><td><span class="tag">{{a.provider||'workbuddy'}}</span></td><td class="mono">{{a.uid?.slice(0,8)}}…</td><td><span class="badge" :class="a.status">{{a.status}}</span></td><td><input class="numctl" v-model.number="a._weight" type="number" min="1" max="100"/></td><td><input class="numctl" v-model.number="a._priority" type="number" min="-100" max="100"/></td><td class="credit-cell"><input class="numctl credit" v-model.number="a._creditSnapshot" type="number" min="0" step="0.01" placeholder="0"/><div class="credit-meta" v-if="a.credit_snapshot>0">余 {{credit(a.credit_remaining)}} · 已用 {{creditPct(a)}}</div><div class="credit-meta" v-else-if="a.total_credits>0">累计消耗(估算) {{credit(a.total_credits)}}</div><div class="credit-meta" v-else>官方失败时可手动校准</div></td><td>{{tokenLife(a)}}</td><td>{{a.total_requests}}</td><td>{{tok(a.total_tokens)}}</td><td>{{credit(a.total_credits)}}</td><td><div class="ops"><button class="btn s" @click="saveMeta(a)" :disabled="not dirty(a)||busyKey(a.id,'save')">{{busyKey(a.id,'save')?'保存中':'保存'}}</button><button class="btn s" @click="toggle(a)" :disabled="busyKey(a.id,'toggle')">{{busyKey(a.id,'toggle')?'处理中':(a.status==='active'?'禁用':'启用')}}</button><button class="btn s" @click="testOne(a)" :disabled="tl===a.id">{{tl===a.id?'测试中':'测试'}}</button><button class="btn s" @click="ref2(a)" :disabled="busyKey(a.id,'refresh')">{{busyKey(a.id,'refresh')?'刷新中':'刷新'}}</button><button class="btn s danger" @click="del(a)">删除</button></div></td></tr>
        <tr v-if="!visibleAccounts.length"><td colspan="12" class="empty">没有匹配的账号</td></tr>
      </tbody></table></div></div>
      <div class="card card-p empty" v-else><div class="em">🔌</div><p>暂无账号 · 登录文件类通道从上方「凭证」区域导入；密钥型通道选中后直接粘贴上游密钥</p></div>

    </div>
  </div>

  <!-- 高级手动添加 modal -->
  <div class="ov" v-if="sa" @click.self="sa=false"><div class="modal"><div class="modal-h"><h3>高级手动添加</h3><button class="x" @click="sa=false">&times;</button></div><div class="modal-b"><div class="field"><label>名称</label><input v-model="nm" placeholder="可选"/></div><div class="field"><label>Auth JSON 或 API Key</label><textarea v-model="ai" placeholder="粘贴 .info 文件内容；单 key 通道可直接粘 API Key / Bearer xxx"></textarea><div class="hint">WorkBuddy 等登录文件通道粘完整 JSON；密钥型通道可直接粘 API Key（自动识别）。</div></div></div><div class="modal-f"><button class="btn" @click="sa=false" :disabled="adding">取消</button><button class="btn pri" @click="add" :disabled="adding">{{adding?'添加中':'添加'}}</button></div></div></div>

  <!-- 测试结果 modal -->
  <div class="ov" v-if="test" @click.self="test=null"><div class="modal"><div class="modal-h"><h3>账号测试 · {{test.account}}</h3><button class="x" @click="test=null">&times;</button></div><div class="modal-b"><div class="testbox"><div class="row"><span>状态</span><span><span class="badge" :class="test.result.ok?'ok':'err'">{{test.result.ok?'成功':'失败'}}</span></span></div><div class="row"><span>HTTP</span><span class="mono">{{test.result.status_code}}</span></div><div class="row"><span>耗时</span><span class="mono">{{test.result.duration_ms}}ms</span></div><div class="row" v-if="test.result.model"><span>模型</span><span class="mono">{{test.result.model}}</span></div><div class="row" v-if="test.result.usage"><span>Token</span><span class="mono">{{tok(test.result.usage.total_tokens)}}</span></div><div class="msg">{{test.result.message||'无返回内容'}}</div></div><div class="hint" style="margin-top:8px">测试会发送一次极短请求，并记录到请求日志。</div></div><div class="modal-f"><button class="btn pri" @click="test=null">关闭</button></div></div></div>
</div>`};