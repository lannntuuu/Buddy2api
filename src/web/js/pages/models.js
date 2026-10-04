import {api,apiErr,respList} from '../api.js';
import {I} from '../icons.js';
const{ref,reactive,computed,onMounted,onUnmounted}=Vue;

export default {props:['token','toast'],setup(p){
  // 统一模型（跨平台翻译层）
  const um=ref([]),umLd=ref(true),umBusy=ref(false),umErr=ref(''),channels=ref([]);
  // 各平台设置（可切换列表）
  const chs=ref([]),chLoaded=ref(false),chErr=ref(''),chBusy=ref({}),activeCh=ref('');
  // Trae SOLO 官方可用模型选择弹窗状态
  const picker=ref({open:false,channel:'',rows:[],busy:false,error:''});

  // 契约 4/6:PUT /admin/channels/{ch}/models 响应带生效模型 id 列表(models),
  // 据此本地回写白名单行;缺 models 或形状不符返回 false → 调用方回退整表 loadAll()。
  function patchChModels(c,r){
    const models=respList(r,'models');
    if(!models)return false;
    c.models=models.slice();
    const have=new Set(models);
    c.modelRows=(c.modelRows||[]).filter(x=>have.has((x.id||'').trim()));
    const kept=new Set(c.modelRows.map(x=>(x.id||'').trim()));
    models.forEach(id=>{if(!kept.has(id))c.modelRows.push({id,names:'',rate:null,display_name:'',official:false,reasoning:(r&&r.reasoning&&r.reasoning[id])||'',maxInput:(r&&r.model_limits&&r.model_limits[id]!==undefined)?r.model_limits[id]:null})});
    // 服务端回显的别名表是权威值：整表刷新「展示名」列，避免本地拼接与后端不一致
    if(r&&typeof r==='object'&&r.aliases){c.aliases=r.aliases;(c.modelRows||[]).forEach(x=>{x.names=aliasNamesFor(c.aliases,(x.id||'').trim())})}
    if(r&&typeof r==='object'&&r.customized)c.customized=r.customized;
    return true;
  }

  async function loadAll(){
    umLd.value=true;chLoaded.value=false;umErr.value='';chErr.value='';
    try{
      const [uv,cr]=await Promise.all([api.get('/admin/unified-models',p.token),api.get('/admin/channels',p.token)]);
      channels.value=(uv.channels&&uv.channels.length)?uv.channels:(cr.channels||[]).filter(c=>c.enabled).map(c=>c.id);
      um.value=(uv.models||[]).map(x=>({name:x.name||'',mappings:{...x.mappings}}));
      const kindById={};(cr.channels||[]).forEach(c=>{kindById[c.id]=c.kind||'builtin'});
      const ids=(cr.channels||[]).filter(c=>c.enabled&&c.loaded).map(c=>c.id);
      chs.value=await Promise.all(ids.map(async id=>{
        try{
          const v=await api.get('/admin/channels/'+id+'/models',p.token);
          const rateById={};(v.model_details||[]).forEach(d=>{rateById[d.id]=d});
          const al=v.aliases||{};
          return {...v,kind:kindById[id]||'builtin',modelRows:(v.models||[]).map(mid=>{const d=rateById[mid]||{};return{id:mid,names:aliasNamesFor(al,mid),rate:d.rate,display_name:d.display_name,official:!!d.official,reasoning:(v.reasoning&&v.reasoning[mid])||'',maxInput:(v.model_limits&&v.model_limits[mid]!==undefined)?v.model_limits[mid]:null,rateLimit:(v.rate_limits&&v.rate_limits[mid])||null}}),reasoningDefault:v.reasoning_default||'',reasoningSupported:!!v.reasoning_supported,reasoningCustomized:!!v.reasoning_customized,sessionModeSupported:!!v.session_mode_supported,sessionMode:v.session_mode||'work',sessionModeDefault:v.session_mode_default||'work',sessionModeCustomized:!!v.session_mode_customized,sessionModeChoices:v.session_mode_choices||['work','code'],defaultMaxInput:(v.default_max_input_tokens!==undefined&&v.default_max_input_tokens!==null)?v.default_max_input_tokens:'',modelLimitsCustomized:!!v.model_limits_customized}
        }catch(e){return{channel:id,kind:kindById[id]||'builtin',error:String(e.message),modelRows:[],aliases:{}}}
      }));
      if(!chs.value.some(c=>c.channel===activeCh.value))activeCh.value=chs.value.length?chs.value[0].channel:'';
    }catch(e){umErr.value=apiErr(e,'加载失败')}
    umLd.value=false;chLoaded.value=true;
  }

  function chOf(){return chs.value.find(c=>c.channel===activeCh.value)}
  function chBusyOf(c){return !!chBusy.value[c.channel]}
  function setChBusy(c,b){chBusy.value={...chBusy.value,[c.channel]:b}}
  function chDefaultText(c){return (c.defaults&&c.defaults.models||[]).join(', ')||'无'}
  function addModelRow(c){c.modelRows.push({id:'',names:'',maxInput:null})}
  function rmModelRow(c,i){c.modelRows.splice(i,1)}
  // 某模型 id 当前的全部别名（对外名），逗号连接——直接编辑"展示名"列即可改。
  function aliasNamesFor(aliases,id){return Object.entries(aliases||{}).filter(([,t])=>t===id).map(([k])=>k).join(', ')}
  // 保存时把「展示名列」的内容翻回别名表：
  //   每行 names 拆成多个别名 → 该行 id；再保留"目标不在白名单"的孤儿别名
  //   （如 workbuddy 的 gpt-5.5→glm-5.2 指向已下架模型），避免被静默丢弃。
  function aliasesFromRows(c){
    const out={};const ids=[];
    (c.modelRows||[]).forEach(r=>{
      const id=(r.id||'').trim();if(!id)return;ids.push(id);
      String(r.names||'').split(',').map(s=>s.trim()).filter(Boolean).forEach(n=>{out[n]=id});
    });
    const wl=new Set(ids);
    Object.entries(c.aliases||{}).forEach(([k,v])=>{if(!wl.has(v)&&!(k in out))out[k]=v});
    return out;
  }

  async function saveChActive(){
    const c=chOf();if(!c||chBusyOf(c))return;
    const models=c.modelRows.map(r=>(r.id||'').trim()).filter(Boolean);
    if(!models.length&&!confirm('确认保存空白名单？这会让 '+c.channel+' 的所有模型请求都 400。点「重置默认」可恢复内置列表。'))return;
    setChBusy(c,true);
    try{
      const al=aliasesFromRows(c);
      const body={models,aliases:al};
      if(c.credit_rate!==undefined&&c.credit_rate!==null)body.credit_rate=Number(c.credit_rate)||0;
      // 会话模式（traework 等支持通道）：仅支持时提交当前选择
      if(c.sessionModeSupported){
        const sm=(c.sessionMode||'').trim();
        if(sm)body.mode=sm;
      }
      // 按模型思考档位：仅收集显式选了档位的行；通道默认单独写 __default__
      if(c.reasoningSupported){
        const reasoning={};
        (c.modelRows||[]).forEach(r=>{const lv=(r.reasoning||'').trim();if(lv)reasoning[r.id]=lv});
        const rd=(c.reasoningDefault||'').trim();
        if(rd)reasoning['__default__']=rd;
        body.reasoning=reasoning;
      }
      // 模型上下文限额（model_limits.json）：仅提交显式填了数字的行；通道默认空串=不提交(null 删除)
      const ml={};
      (c.modelRows||[]).forEach(r=>{const id=(r.id||'').trim();const v=(r.maxInput===null||r.maxInput===undefined||r.maxInput==='')?null:parseInt(r.maxInput,10);if(id&&v!==null&&!Number.isNaN(v)&&v>0)ml[id]=v});
      body.model_limits=ml;
      const dmi=parseInt(c.defaultMaxInput,10);
      body.default_max_input_tokens=(c.defaultMaxInput===''||c.defaultMaxInput===null||Number.isNaN(dmi)||dmi<=0)?null:dmi;
      const r=await api.put('/admin/channels/'+c.channel+'/models',body,p.token);
      // 契约 4/6:生效模型列表本地回写;形状不符回退整表 loadAll()
      if(patchChModels(c,r))p.toast(c.channel+' 已保存');
      else{p.toast(c.channel+' 已保存');await loadAll()}
    }catch(e){p.toast('保存失败：'+apiErr(e),'err')}
    setChBusy(c,false);
  }
  async function resetChActive(){
    const c=chOf();if(!c||chBusyOf(c))return;
    if(!confirm('将 '+c.channel+' 的模型列表/别名/思考档位/上下文限额重置为内置默认？'))return;
    setChBusy(c,true);
    try{await api.put('/admin/channels/'+c.channel+'/models',{models:null,aliases:null,credit_rate:null,reasoning:null,mode:null,model_limits:{},default_max_input_tokens:null},p.token);p.toast(c.channel+' 已重置为默认');await loadAll()}
    catch(e){p.toast('重置失败：'+apiErr(e),'err')}
    setChBusy(c,false);
  }
  function canRefreshOfficial(c){return c&&(c.kind==='apikey'||c.channel==='traesolo'||c.channel==='workbuddy')}
  async function refreshOfficialModels(){
    const c=chOf();if(!c||chBusyOf(c)||!canRefreshOfficial(c))return;
    setChBusy(c,true);
    try{
      const r=await api.post('/admin/channels/'+c.channel+'/models/refresh',{},p.token,{timeoutMs:60000});
      // Trae SOLO / WorkBuddy：直接弹出官方可用模型选择弹窗
      if(c.channel==='traesolo'||c.channel==='workbuddy'){
        if(r&&r.refreshed&&Array.isArray(r.official_models)&&r.official_models.length){
          openPicker(c,r.official_models);
        }else{
          p.toast('刷新失败：'+(r&&r.note?r.note:'无可用账号或上游不可达'),'err');
        }
        return;
      }
      // 其余（密钥型 apikey）通道：保持原行为
      if(r&&r.refreshed){p.toast(c.channel+' 官方模型表已刷新')}
      else{p.toast((r&&r.note)||(c.channel+' 刷新未完成'),'info')}
      await loadAll();
    }catch(e){p.toast('刷新失败：'+apiErr(e),'err')}
    // 必须用 finally 复位忙碌态：上面 traesolo 分支会 return，若把
    // setChBusy 放在函数末尾会被跳过，按钮将永久卡在「刷新中」且禁用。
    finally{setChBusy(c,false)}
  }
  function openPicker(c,official){
    const wl=new Set((c.models||[]).map(x=>String(x).toLowerCase()));
    picker.value={
      open:true,
      channel:c.channel,
      error:'',
      busy:false,
      rows:(official||[]).map(m=>({
        id:m.id,
        display_name:m.display_name||'',
        rate:(m.rate===null||m.rate===undefined)?null:m.rate,
        context_window:(m.context_window===null||m.context_window===undefined)?null:m.context_window,
        checked:wl.has(String(m.id).toLowerCase())
      }))
    };
  }
  function closePicker(){picker.value.open=false}
  function pickerToggleAll(v){picker.value.rows.forEach(r=>{r.checked=v});}
  // 收集勾选的 id（保持官方顺序），并清理指向已剔除模型的孤儿别名。
  // 大小写不敏感判断别名目标是否在 models 中，但写回值保持原样。
  function pickerSavePayload(c){
    const models=picker.value.rows.filter(r=>r.checked).map(r=>r.id);
    const wl=new Set(models.map(x=>String(x).toLowerCase()));
    const al=c.aliases||{};
    const aliases={};
    Object.keys(al).forEach(k=>{const v=al[k];if(wl.has(String(v).toLowerCase()))aliases[k]=v});
    return {models,aliases};
  }
  async function savePicker(){
    const c=chOf();if(!c||picker.value.busy)return;
    const {models,aliases}=pickerSavePayload(c);
    if(!models.length&&!confirm('确认保存空白名单？这会让 '+picker.value.channel+' 的所有模型请求都 400。'))return;
    picker.value.busy=true;picker.value.error='';
    try{
      // 仅提交 models 与清理后的 aliases；不动思考档位/上下文限额
      await api.put('/admin/channels/'+picker.value.channel+'/models',{models,aliases},p.token);
      p.toast(picker.value.channel+' 官方模型已保存');
      closePicker();
      await loadAll();
    }catch(e){picker.value.error='保存失败：'+apiErr(e)}
    picker.value.busy=false;
  }

  // 统一模型表操作
  function addUM(){um.value.push({name:'',mappings:{}})}
  function rmUM(i){um.value.splice(i,1)}
  function umCell(r,ch){return (r.mappings||{})[ch]||''}
  function umSet(r,ch,v){const m={...r.mappings};if((v||'').trim())m[ch]=v;else delete m[ch];r.mappings=m}
  function umWarn(r,ch){
    const v=(umCell(r,ch)||'').trim();if(!v)return false;
    const c=chs.value.find(x=>x.channel===ch);if(!c)return false;
    if(ch==='qclaw'&&v.startsWith('pool-'))return false;
    return !(c.models||[]).includes(v);
  }
  async function saveUM(){
    const names={};
    for(const r of um.value){
      const n=(r.name||'').trim(),hasMap=Object.keys(r.mappings||{}).length;
      if(!n&&!hasMap)continue;
      if(!n){p.toast('统一模型名不能为空','err');return}
      if(!hasMap){p.toast('统一模型 '+n+' 还没有任何平台映射','err');return}
      if(names[n]){p.toast('统一模型名重复：'+n,'err');return}
      names[n]=1;
    }
    umBusy.value=true;
    try{
      const clean=um.value.filter(r=>(r.name||'').trim()&&Object.keys(r.mappings||{}).length)
        .map(r=>({name:r.name.trim(),mappings:{...r.mappings}}));
      await api.put('/admin/unified-models',{models:clean},p.token);
      p.toast('统一模型已保存');
      await loadAll();
    }catch(e){p.toast('保存失败：'+apiErr(e),'err')}
    umBusy.value=false;
  }

  // 6004 (账号,模型) 级限流展示：最早恢复时间 + hover 各账号明细（纯内存态，
  // 重启即清）。fmt 直接来自后端 reset_at_iso，避免前端时区换算错位。
  // 限流会随时间自动解除且无推送，60s 轮询刷新一次快照即可接受。
  function rlEarliest(r){
    const rl=r.rateLimit;if(!rl||!rl.earliest_reset)return '';
    return rl.earliest_reset_iso||(new Date(rl.earliest_reset*1000)).toLocaleString();
  }
  function rlDetail(r){
    const rl=r.rateLimit;if(!rl||!rl.limited_accounts||!rl.limited_accounts.length)return '';
    return rl.limited_accounts.map(a=>'#'+a.account_id+(a.account_name?' '+a.account_name:'')+' · '+(a.reset_at_iso||'')).join('\n');
  }
  let rlTimer=null;
  async function refreshRateLimits(){
    if(document.hidden)return;
    try{
      const v=await api.get('/admin/channels/'+activeCh.value+'/models',p.token);
      const byId={};(v.models||[]).forEach(mid=>{byId[mid]=(v.rate_limits&&v.rate_limits[mid])||null});
      const c=chOf();if(!c)return;
      c.modelRows.forEach(r=>{r.rateLimit=byId[r.id]!==undefined?byId[r.id]:null});
    }catch(e){/* 快照刷新失败不打扰 */}
  }
  onMounted(loadAll);
  onMounted(()=>{rlTimer=setInterval(refreshRateLimits,60000)});
  onUnmounted(()=>{clearInterval(rlTimer)});
  return{um,umLd,umErr,umBusy,channels,addUM,rmUM,umCell,umSet,umWarn,saveUM,chs,chLoaded,chErr,activeCh,chOf,chBusyOf,addModelRow,rmModelRow,chDefaultText,saveChActive,resetChActive,canRefreshOfficial,refreshOfficialModels,openPicker,closePicker,pickerToggleAll,savePicker,picker,rlEarliest,rlDetail,I}
},template:`
<div>
  <div class="phead"><h1>模型配置</h1><p>统一模型翻译 · 各通道白名单与别名 · 改动即时生效</p></div>
  <div class="card"><div class="card-h">统一模型<span class="sub">统一名以 WorkBuddy 命名为准 · 纯翻译层 · 各平台白名单仍是最终闸门</span><div style="margin-left:auto;display:flex;gap:6px"><button class="btn s" @click="addUM"><span v-html="I.plus"></span>添加统一模型</button><button class="btn s pri" @click="saveUM" :disabled="umBusy">{{umBusy?'保存中…':'保存统一模型'}}</button></div></div>
    <div v-if="umLd" class="load"><div class="spin"></div></div>
    <div v-else-if="umErr" style="padding:16px;color:var(--err);font-size:12px">{{umErr}}</div>
    <div v-else class="table-scroll"><table>
      <thead><tr><th style="min-width:190px">统一模型名（客户端请求这个）</th><th v-for="ch in channels" :key="ch" style="min-width:170px">{{ch}}</th><th style="width:64px"></th></tr></thead>
      <tbody>
        <tr v-for="(r,i) in um" :key="i">
          <td><input class="tcell" v-model="r.name" placeholder="如 deepseek-v4-flash"/></td>
          <td v-for="ch in channels" :key="ch"><input class="tcell" :class="{warn:umWarn(r,ch)}" :value="umCell(r,ch)" @input="umSet(r,ch,$event.target.value)" placeholder="该平台无"/></td>
          <td><button class="btn s danger" @click="rmUM(i)">删除</button></td>
        </tr>
        <tr v-if="!um.length"><td :colspan="channels.length+2" class="empty">暂无统一模型。添加后客户端直接请求统一名，网关自动翻译成各平台内部名（例：请求 deepseek-v4-flash → TraeWork 实际打 DeepSeek-V4-Flash-Official）</td></tr>
      </tbody>
    </table></div>
    <div style="padding:10px 16px;font-size:11px;color:var(--fg3);border-top:1px solid var(--border-strong)">格子 = 该平台内部模型名（该平台没有则留空）；<span style="color:var(--err)">红框</span> = 内部名不在该平台当前白名单内，请求会 400</div>
  </div>
  <div class="card" style="margin-top:16px"><div class="card-h">各平台设置<span class="sub">每平台独立的模型白名单与别名</span><select v-if="chs.length" v-model="activeCh" class="selectctl" style="margin-left:auto"><option v-for="c in chs" :key="c.channel" :value="c.channel">{{c.channel}}</option></select></div>
    <div v-if="!chLoaded" class="load"><div class="spin"></div></div>
    <div v-else-if="chErr" style="padding:16px;color:var(--err);font-size:12px">{{chErr}}</div>
    <div v-else-if="!chOf()" class="empty">没有已加载的通道</div>
    <div v-else class="card-p">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;gap:8px;flex-wrap:wrap">
        <div style="display:flex;align-items:center">
          <strong style="font-family:var(--mono)">{{chOf().channel}}</strong>
          <span v-if="chOf().customized&&(chOf().customized.models||chOf().customized.aliases)" class="tag" style="margin-left:8px">自定义</span>
          <span v-else class="tag" style="margin-left:8px">默认</span>
          <span style="color:var(--fg3);font-size:12px;margin-left:8px">{{(chOf().models||[]).length}} 个模型生效</span>
        </div>
        <div style="display:flex;gap:6px">
          <button class="btn s pri" @click="saveChActive" :disabled="chBusyOf(chOf())">{{chBusyOf(chOf())?'保存中':'保存'}}</button>
          <button class="btn s" @click="resetChActive" :disabled="chBusyOf(chOf())">重置默认</button>
        </div>
      </div>
      <div style="margin-bottom:14px"><label style="font-size:12px;color:var(--fg-2);display:block;margin-bottom:6px">模型白名单（保存 = 按列表整体保存；空白名单保存 = 该平台所有模型请求 400；列表外的模型 400）<span v-if="canRefreshOfficial(chOf())&&chOf().channel==='traesolo'" style="margin-left:8px;color:var(--fg3)">· 倍率来自官方 consumption_rate（原值）</span><span v-else-if="chOf()&&chOf().channel==='workbuddy'" style="margin-left:8px;color:var(--fg3)">· 倍率来自官方 /v3/config credits（x 系数）</span><span v-else-if="canRefreshOfficial(chOf())" style="margin-left:8px;color:var(--fg3)">· 倍率来自上游 /v1/models</span><span v-else style="margin-left:8px;color:var(--fg3)">· 该通道上游不提供倍率，显示「-」</span></label>
        <div class="hint" style="margin:0 0 8px">「展示名」就是 <code>GET /v1/models</code> 列出的名字，也是客户端该请求的名字（保存后即时生效）；留空则直接用模型 ID。多个名字用英文逗号分隔。</div>
        <div v-if="chOf().modelRows.length" class="table-scroll" style="margin-bottom:8px">
          <table style="font-size:12px">
            <thead><tr><th style="text-align:left;padding:4px 8px">模型 ID</th><th style="text-align:left;padding:4px 8px;min-width:90px">展示名</th><th style="text-align:right;padding:4px 8px;min-width:90px">倍率</th><th style="text-align:left;padding:4px 8px;min-width:130px">最大输入上下文</th><th v-if="chOf().reasoningSupported" style="text-align:left;padding:4px 8px;min-width:118px">思考档位</th><th v-if="chOf().channel==='workbuddy'" style="text-align:left;padding:4px 8px;min-width:150px" title="上游 6004 频率限制的解除时间（进程内状态，重启后清空）">限流解除时间</th><th style="width:56px"></th></tr></thead>
            <tbody>
              <tr v-for="(r,i) in chOf().modelRows" :key="i">
                <td><input class="tcell" v-model="r.id" placeholder="模型 ID"/></td>
                <td><input class="tcell" v-model="r.names" :placeholder="r.display_name&&r.display_name!==r.id?r.display_name:r.id" style="font-family:var(--mono)"/></td>
                <td style="padding:3px 8px;text-align:right;font-family:var(--mono)">
                  <span v-if="r.rate!==null&&r.rate!==undefined">{{r.rate}}</span>
                  <span v-else style="color:var(--fg3)">-</span>
                  <span v-if="r.official" title="官方接口提供" style="color:var(--ok);font-size:10px;margin-left:4px">●</span>
                </td>
                <td style="padding:3px 8px">
                  <input class="tcell" v-model.number="r.maxInput" type="number" min="1" step="1" style="width:130px;text-align:right;font-family:var(--mono)" :placeholder="chOf().defaultMaxInput!==''?('默认 '+chOf().defaultMaxInput):''"/>
                </td>
                <td v-if="chOf().reasoningSupported" style="padding:3px 8px">
                  <select v-model="r.reasoning" class="selectctl" style="padding:4px 6px;font-size:12px">
                    <option value="">默认（不注入）</option>
                    <option v-for="lv in ['none','minimal','low','medium','high','max']" :key="lv" :value="lv">{{lv}}</option>
                  </select>
                </td>
                <td v-if="chOf().channel==='workbuddy'" style="padding:3px 8px;font-family:var(--mono)">
                  <span v-if="rlEarliest(r)" class="tag" style="color:var(--warn,#d97706);cursor:default" :title="rlDetail(r)">⏳ {{rlEarliest(r)}}</span>
                  <span v-else style="color:var(--fg3)">-</span>
                </td>
                <td style="padding:3px 8px;text-align:right"><button class="btn s danger" @click="rmModelRow(chOf(),i)">删除</button></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="empty" style="padding:10px 8px">当前无模型（保存空白名单会让该平台所有请求 400）。</div>
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
          <div style="display:flex;gap:6px">
            <button class="btn s" @click="addModelRow(chOf())" style="font-size:11px;padding:3px 10px"><span v-html="I.plus"></span>添加模型</button>
            <button v-if="canRefreshOfficial(chOf())" class="btn s" @click="refreshOfficialModels(chOf())" :disabled="chBusyOf(chOf())"><span v-html="I.refresh"></span>{{chBusyOf(chOf())?'刷新中':'刷新官方模型表'}}</button>
          </div>
          <div class="hint" style="margin:0">内置默认：{{chDefaultText(chOf())}}</div>
        </div>
      </div>
      <div v-if="chOf().reasoningSupported" style="margin-top:14px;border-top:1px dashed var(--border);padding-top:12px">
        <label style="font-size:12px;color:var(--fg-2);display:block;margin-bottom:6px">思考档位（通道默认） · 最大输入上下文（通道默认）</label>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <select v-model="chOf().reasoningDefault" class="selectctl" style="padding:4px 6px;font-size:12px">
            <option value="">默认（不注入，跟随上游）</option>
            <option v-for="lv in ['none','minimal','low','medium','high','max']" :key="lv" :value="lv">{{lv}}</option>
          </select>
          <input class="tcell" v-model.number="chOf().defaultMaxInput" type="number" min="1" step="1" style="width:150px;font-family:var(--mono)" placeholder="全局默认 1048576"/>
          <span v-if="chOf().reasoningCustomized" class="tag" style="margin-top:6px">已自定义思考档位</span>
          <span v-if="chOf().modelLimitsCustomized" class="tag" style="margin-top:6px">已自定义上下文限额</span>
        </div>
        <div style="font-size:11px;color:var(--fg3);margin-top:6px">客户端显式传 <code style="font:inherit">reasoning_effort</code> 始终优先；上方每模型下拉可单独覆盖。实测：deepseek/glm/auto 默认不思考、选档位=开启思考；kimi 默认轻思考、选 low 可减少；想要最快可给 DeepSeek 选 low 或留空。上下文限额留空 = 跟随全局默认（1048576）；每模型列可单独覆盖，空 = 未配置（跟随通道/全局默认）；超限请求会被 400 拒绝。</div>
      </div>
      <div v-else style="margin-top:14px;border-top:1px dashed var(--border);padding-top:12px">
        <label style="font-size:12px;color:var(--fg-2);display:block;margin-bottom:6px">最大输入上下文（通道默认）</label>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <input class="tcell" v-model.number="chOf().defaultMaxInput" type="number" min="1" step="1" style="width:150px;font-family:var(--mono)" placeholder="全局默认 1048576"/>
          <span v-if="chOf().modelLimitsCustomized" class="tag" style="margin-top:6px">已自定义上下文限额</span>
        </div>
        <div style="font-size:11px;color:var(--fg3);margin-top:6px">留空 = 跟随全局默认（1048576）；每模型列可单独覆盖，空 = 未配置；超限请求会被 400 拒绝。</div>
      </div>
      <div v-if="chOf().sessionModeSupported" style="margin-top:14px;border-top:1px dashed var(--border);padding-top:12px">
        <label style="font-size:12px;color:var(--fg-2);display:block;margin-bottom:6px">会话模式（TraeWork）</label>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <select v-model="chOf().sessionMode" class="selectctl" style="padding:4px 6px;font-size:12px">
            <option v-for="m in chOf().sessionModeChoices" :key="m" :value="m">{{m==='work'?'work（工作）':(m==='code'?'code（代码）':m)}}</option>
          </select>
          <span v-if="chOf().sessionModeCustomized" class="tag" style="margin-top:6px">已自定义会话模式</span>
        </div>
        <div style="font-size:11px;color:var(--fg3);margin-top:6px">code = 走官方 TRAE Code agent（solo_agent_lite）；work = 官方 TRAE Work（默认）。改动即时生效、无需重启。</div>
      </div>
      <div style="margin-top:14px;border-top:1px dashed var(--border);padding-top:12px"><label style="font-size:12px;color:var(--fg-2);display:block;margin-bottom:6px">相对消耗缩放因子（tokens ÷ 该值 × 模型倍率）</label>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <input class="tcell" style="width:120px" v-model="chOf().credit_rate" type="number" min="0" step="1"/>
          <span class="hint" style="margin:0" v-if="chOf().channel==='traesolo'">TRAE SOLO 已改用<strong>官方三档标价公式</strong>（input/cache_read/output 分别计价，反解自官方 session 真值，46/51 行误差<1%，见 pricing.py）。本栏缩放因子仅在请求无 token 数据时兜底使用。注意：标价≠实际扣费，订阅内官方实际扣费远低于标价（见 docs §10.5）。</span>
          <span class="hint" style="margin:0" v-else-if="chOf().channel==='traework'">TraeWork 消耗已改用<strong>官方 session 真值</strong>（query_user_usage_group_by_session，每小时自动同步），不再走 token 估算。本栏缩放因子对 TraeWork 不生效；dashboard 的 TraeWork 每日 credit 显示的是官方真积分。</span>
          <span class="hint" style="margin:0" v-else-if="chOf().channel==='qodercn'">Qoder <strong>不需要倍率</strong>：上游每次都在 usage 里回报本次真实扣费（<code style="font:inherit">credits</code>）与是否计费（<code style="font:inherit">billable</code>），消耗统计直接取真值。免费档（Qwen3.8-Flash）<code style="font:inherit">billable=false</code> → 记 0，不再按 token 估算。本栏缩放因子仅在该通道没有 usage 时才兜底。</span>
          <span class="hint" style="margin:0" v-else>上游不回报 credit 的通道（qclaw/qwenwork）用「token 数 ÷ 该值」近似统计消耗；留 0 或不填 = 不做估算。内置默认 {{chOf().credit_rate_default}}。</span>
        </div>
        <div v-if="chOf().credit_rate_customized" class="tag" style="margin-top:6px">已自定义换算率</div>
      </div>
      <div v-if="chOf().error" style="margin-top:10px;font-size:12px;color:var(--err)">{{chOf().error}}</div>
    </div>
  </div>
  <div class="ov" v-if="picker.open" @click.self="closePicker()">
    <div class="modal wide" style="width:880px;max-width:94vw;display:flex;flex-direction:column;max-height:88vh">
      <div class="modal-h">
        <div>
          <h3>Trae SOLO 官方可用模型</h3>
          <div class="hint" style="margin:4px 0 0">勾选要启用的模型 · 保存后写入该通道白名单（思考档位 / 上下文限额不变）</div>
        </div>
        <button class="x" @click="closePicker()">&times;</button>
      </div>
      <div class="modal-b" style="overflow:auto">
        <div v-if="picker.error" style="margin-bottom:10px;font-size:12px;color:var(--err)">{{picker.error}}</div>
        <div style="display:flex;gap:6px;margin-bottom:8px">
          <button class="btn s" @click="pickerToggleAll(true)" :disabled="picker.busy">全选</button>
          <button class="btn s" @click="pickerToggleAll(false)" :disabled="picker.busy">全不选</button>
          <span class="hint" style="margin:0;align-self:center">已选 {{picker.rows.filter(r=>r.checked).length}} / {{picker.rows.length}}</span>
        </div>
        <div class="table-scroll" style="margin:0">
          <table style="font-size:12px">
            <thead><tr><th style="text-align:left;padding:4px 8px;width:48px">启用</th><th style="text-align:left;padding:4px 8px;min-width:180px">展示名</th><th style="text-align:left;padding:4px 8px;min-width:200px">模型 ID</th><th style="text-align:right;padding:4px 8px;min-width:80px">倍率</th><th style="text-align:right;padding:4px 8px;min-width:120px">上下文窗口</th></tr></thead>
            <tbody>
              <tr v-for="(r,i) in picker.rows" :key="r.id">
                <td style="padding:3px 8px;text-align:center"><input type="checkbox" v-model="r.checked" :disabled="picker.busy"/></td>
                <td style="padding:3px 8px">{{r.display_name||r.id}}</td>
                <td style="padding:3px 8px;font-family:var(--mono)">{{r.id}}</td>
                <td style="padding:3px 8px;text-align:right;font-family:var(--mono)">
                  <span v-if="r.rate!==null&&r.rate!==undefined">{{r.rate}}</span>
                  <span v-else style="color:var(--fg3)">-</span>
                </td>
                <td style="padding:3px 8px;text-align:right;font-family:var(--mono)">
                  <span v-if="r.context_window!==null&&r.context_window!==undefined">{{r.context_window}}</span>
                  <span v-else style="color:var(--fg3)">-</span>
                </td>
              </tr>
              <tr v-if="!picker.rows.length"><td :colspan="5" class="empty">无可用官方模型</td></tr>
            </tbody>
          </table>
        </div>
      </div>
      <div class="modal-f">
        <button class="btn" @click="closePicker()" :disabled="picker.busy">取消</button>
        <button class="btn pri" @click="savePicker()" :disabled="picker.busy">{{picker.busy?'保存中…':'保存'}}</button>
      </div>
    </div>
  </div>
</div>`};