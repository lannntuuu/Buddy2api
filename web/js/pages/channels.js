import {api,apiErr} from '../api.js';
import {I} from '../icons.js';
const{ref,reactive,computed,onMounted}=Vue;

export default {props:['token','toast'],setup(p){
  // 统一模型（跨平台翻译层）
  const um=ref([]),umLd=ref(true),umBusy=ref(false),umErr=ref(''),channels=ref([]);
  // 各平台设置（可切换列表）
  const chs=ref([]),chLoaded=ref(false),chErr=ref(''),chBusy=ref({}),activeCh=ref('');

  async function loadAll(){
    umLd.value=true;chLoaded.value=false;umErr.value='';chErr.value='';
    try{
      const [uv,cr]=await Promise.all([api.get('/admin/unified-models',p.token),api.get('/admin/channels',p.token)]);
      channels.value=(uv.channels&&uv.channels.length)?uv.channels:(cr.channels||[]).map(c=>c.id);
      um.value=(uv.models||[]).map(x=>({name:x.name||'',mappings:{...x.mappings}}));
      const ids=(cr.channels||[]).filter(c=>c.enabled&&c.loaded).map(c=>c.id);
      chs.value=await Promise.all(ids.map(async id=>{
        try{
          const v=await api.get('/admin/channels/'+id+'/models',p.token);
          return {...v,modelRows:(v.models||[]).map(mid=>({id:mid})),aliasRows:Object.entries(v.aliases||{}).map(([k,val])=>({k,v:val}))}
        }catch(e){return{channel:id,error:String(e.message),modelRows:[],aliasRows:[]}}
      }));
      if(!chs.value.some(c=>c.channel===activeCh.value))activeCh.value=chs.value.length?chs.value[0].channel:'';
    }catch(e){umErr.value=apiErr(e,'加载失败')}
    umLd.value=false;chLoaded.value=true;
  }

  function chOf(){return chs.value.find(c=>c.channel===activeCh.value)}
  function chBusyOf(c){return !!chBusy.value[c.channel]}
  function setChBusy(c,b){chBusy.value={...chBusy.value,[c.channel]:b}}
  function chDefaultText(c){return (c.defaults&&c.defaults.models||[]).join(', ')||'无'}
  function addRow(c){c.aliasRows.push({k:'',v:''})}
  function rmRow(c,i){c.aliasRows.splice(i,1)}
  function addModelRow(c){c.modelRows.push({id:''})}
  function rmModelRow(c,i){c.modelRows.splice(i,1)}

  async function saveChActive(){
    const c=chOf();if(!c||chBusyOf(c))return;
    const models=c.modelRows.map(r=>(r.id||'').trim()).filter(Boolean);
    if(!models.length&&!confirm('确认保存空白名单？这会让 '+c.channel+' 的所有模型请求都 400。点「重置默认」可恢复内置列表。'))return;
    setChBusy(c,true);
    try{
      const al={};(c.aliasRows||[]).forEach(r=>{const k=(r.k||'').trim(),v=(r.v||'').trim();if(k&&v)al[k]=v});
      const body={models,aliases:al};
      if(c.credit_rate!==undefined&&c.credit_rate!==null)body.credit_rate=Number(c.credit_rate)||0;
      await api.put('/admin/channels/'+c.channel+'/models',body,p.token);
      p.toast(c.channel+' 已保存');await loadAll();
    }catch(e){p.toast('保存失败：'+apiErr(e),'err')}
    setChBusy(c,false);
  }
  async function resetChActive(){
    const c=chOf();if(!c||chBusyOf(c))return;
    if(!confirm('将 '+c.channel+' 的模型列表/别名重置为内置默认？'))return;
    setChBusy(c,true);
    try{await api.put('/admin/channels/'+c.channel+'/models',{models:  null,aliases:null,credit_rate:null},p.token);p.toast(c.channel+' 已重置为默认');await loadAll()}
    catch(e){p.toast('重置失败：'+apiErr(e),'err')}
    setChBusy(c,false);
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

  onMounted(loadAll);return{um,umLd,umErr,umBusy,channels,addUM,rmUM,umCell,umSet,umWarn,saveUM,chs,chLoaded,chErr,activeCh,chOf,chBusyOf,addRow,rmRow,addModelRow,rmModelRow,chDefaultText,saveChActive,resetChActive,I}
},template:`
<div>
  <div class="phead"><h1>通道与模型</h1><p>通道视角集中管理：统一模型翻译 · 各通道白名单与别名 · 改动即时生效</p></div>
  <div class="card"><div class="card-h">统一模型<span class="sub">统一名以 WorkBuddy 命名为准 · 纯翻译层 · 各平台白名单仍是最终闸门</span><div style="margin-left:auto;display:flex;gap:6px"><button class="btn s" @click="addUM"><span v-html="I.plus"></span>添加统一模型</button><button class="btn s pri" @click="saveUM" :disabled="umBusy">{{umBusy?'保存中…':'保存统一模型'}}</button></div></div>
    <div v-if="umLd" class="load"><div class="spin"></div></div>
    <div v-else-if="umErr" style="padding:16px;color:var(--red);font-size:12px">{{umErr}}</div>
    <div v-else class="table-scroll"><table>
      <thead><tr><th style="min-width:190px">统一模型名（客户端请求这个）</th><th v-for="ch in channels" :key="ch" style="min-width:170px">{{ch}}</th><th style="width:64px"></th></tr></thead>
      <tbody>
        <tr v-for="(r,i) in um" :key="i">
          <td><input v-model="r.name" placeholder="如 deepseek-v4-flash" style="width:100%;padding:6px 9px;border:1px solid #e8e8e8;border-radius:4px;font:inherit;font-size:12px;font-family:var(--mono);background:#fff;outline:none"/></td>
          <td v-for="ch in channels" :key="ch"><input :value="umCell(r,ch)" @input="umSet(r,ch,$event.target.value)" placeholder="该平台无" :style="umWarn(r,ch)?{borderColor:'var(--red)',background:'var(--red-bg)'}:{}" style="width:100%;padding:6px 9px;border:1px solid #e8e8e8;border-radius:4px;font:inherit;font-size:12px;font-family:var(--mono);background:#fff;outline:none"/></td>
          <td><button class="btn s danger" @click="rmUM(i)">删除</button></td>
        </tr>
        <tr v-if="!um.length"><td :colspan="channels.length+2" class="empty">暂无统一模型。添加后客户端直接请求统一名，网关自动翻译成各平台内部名（例：请求 deepseek-v4-flash → TraeWork 实际打 DeepSeek-V4-Flash-Official）</td></tr>
      </tbody>
    </table></div>
    <div style="padding:10px 16px;font-size:11px;color:var(--fg3);border-top:1px solid var(--border2)">格子 = 该平台内部模型名（该平台没有则留空）；<span style="color:var(--red)">红框</span> = 内部名不在该平台当前白名单内，请求会 400</div>
  </div>
  <div class="card" style="margin-top:16px"><div class="card-h">各平台设置<span class="sub">每平台独立的模型白名单与别名</span><select v-if="chs.length" v-model="activeCh" class="selectctl" style="margin-left:auto"><option v-for="c in chs" :key="c.channel" :value="c.channel">{{c.channel}}</option></select></div>
    <div v-if="!chLoaded" class="load"><div class="spin"></div></div>
    <div v-else-if="chErr" style="padding:16px;color:var(--red);font-size:12px">{{chErr}}</div>
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
      <div style="margin-bottom:14px"><label style="font-size:12px;color:var(--fg2);display:block;margin-bottom:6px">模型白名单（保存 = 按列表整体保存；空白名单保存 = 该平台所有模型请求 400；列表外的模型 400）</label>
        <div v-for="(r,i) in chOf().modelRows" :key="i" style="display:flex;gap:8px;margin-bottom:6px;align-items:center">
          <input v-model="r.id" placeholder="模型 ID" style="flex:1;padding:5px 8px;border:1px solid #e8e8e8;border-radius:4px;font:inherit;font-size:12px;font-family:var(--mono);background:#fff;outline:none"/>
          <button class="btn s danger" @click="rmModelRow(chOf(),i)">删除</button>
        </div>
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
          <button class="btn s" @click="addModelRow(chOf())" style="font-size:11px;padding:3px 10px"><span v-html="I.plus"></span>添加模型</button>
          <div class="hint" style="margin:0">内置默认：{{chDefaultText(chOf())}}</div>
        </div>
      </div>
      <div><label style="font-size:12px;color:var(--fg2);display:block;margin-bottom:6px">别名（别名 → 模型 ID；保存 = 按列表整体保存；删空后保存 = 该平台无任何别名）</label>
        <div v-for="(r,i) in chOf().aliasRows" :key="i" style="display:flex;gap:8px;margin-bottom:6px;align-items:center">
          <input v-model="r.k" placeholder="别名 (如 auto)" style="flex:1;padding:5px 8px;border:1px solid #e8e8e8;border-radius:4px;font:inherit;font-size:12px;font-family:var(--mono);background:#fff;outline:none"/>
          <span style="color:var(--fg3)">→</span>
          <input v-model="r.v" placeholder="模型 ID" style="flex:1;padding:5px 8px;border:1px solid #e8e8e8;border-radius:4px;font:inherit;font-size:12px;font-family:var(--mono);background:#fff;outline:none"/>
          <button class="btn s danger" @click="rmRow(chOf(),i)">删除</button>
        </div>
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
          <button class="btn s" @click="addRow(chOf())" style="font-size:11px;padding:3px 10px"><span v-html="I.plus"></span>添加别名</button>
          <div class="hint" style="margin:0">内置默认别名：{{Object.entries((chOf().defaults&&chOf().defaults.aliases)||{}).map(([k,v])=>k+'→'+v).join(', ')||'无'}}</div>
        </div>
      </div>
      <div style="margin-top:14px;border-top:1px dashed #e2e2e2;padding-top:12px"><label style="font-size:12px;color:var(--fg2);display:block;margin-bottom:6px">Credit 估算换算率（tokens / 1 credit）</label>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <input v-model="chOf().credit_rate" type="number" min="0" step="1" style="width:120px;padding:5px 8px;border:1px solid #e8e8e8;border-radius:4px;font:inherit;font-size:12px;font-family:var(--mono);background:#fff;outline:none"/>
          <span class="hint" style="margin:0">上游不回报 credit 的通道（traesolo/qclaw/qwenwork）用「token 数 ÷ 该值」近似统计消耗；留 0 或不填 = 不做估算。内置默认 {{chOf().credit_rate_default}}。</span>
        </div>
        <div v-if="chOf().credit_rate_customized" class="tag" style="margin-top:6px">已自定义换算率</div>
      </div>
      <div v-if="chOf().error" style="margin-top:10px;font-size:12px;color:var(--red)">{{chOf().error}}</div>
    </div>
  </div>
</div>`};
