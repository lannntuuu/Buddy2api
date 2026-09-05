import {api,apiErr,n,tok,money,ms,pct} from '../api.js';
import {I} from '../icons.js';
const{ref,reactive,computed,onMounted}=Vue;
const RATIO_TIP='缓存命中 Token ÷ 输入 Token(prompt_tokens) — 上游 prompt 已含缓存命中部分;输入为 0 时不计算';
const AVG_TIP='总耗时 ÷ 请求数 — 全部请求的平均值,含失败';
const CACHE_INC_TIP='已包含在输入 Token(prompt_tokens)中,非额外增量';

export default {props:['token','toast'],setup(p){
  const data=ref(null),ld=ref(false),err=ref('');
  const f=reactive({range:'1',days:1,start:'',end:'',provider:'',model:''});
  const channels=ref([]);
  const channelModels=ref({});
  function rangePreset(k){f.range=k;if(k!=='custom'){f.days=Number(k);f.start='';f.end=''}else{f.days=null}load()}
  function onProviderChange(){f.model='';load()}
  async function loadChannels(){try{const ch=await api.get('/admin/channels',p.token);channels.value=ch.channels||[]}catch(e){channels.value=[]}}
  async function loadProviderModels(channel){if(!channel)return;try{const r=await api.get('/admin/channels/'+channel+'/models',p.token);channelModels.value={...channelModels.value,[channel]:r.models||[]}}catch(e){channelModels.value={...channelModels.value,[channel]:[]}}}
  function qs(){const u=new URLSearchParams();if(f.provider)u.set('provider',f.provider);if(f.model)u.set('model',f.model);if(f.range==='custom'){if(f.start)u.set('start_date',f.start);if(f.end)u.set('end_date',f.end)}else u.set('days',f.days!=null?f.days:Number(f.range)||7);return u.toString()}
  async function load(){ld.value=true;err.value='';try{if(f.provider&&!channelModels.value[f.provider])await loadProviderModels(f.provider);data.value=await api.get('/admin/provider-model-usage?'+qs(),p.token)}catch(e){let msg=apiErr(e,'用量加载失败');try{const r=await fetch('/admin/provider-model-usage?'+qs(),{headers:p.token?{Authorization:'Bearer '+p.token}:{},credentials:'same-origin'});if(r.status===400){const j=await r.json();msg=j.detail||msg}}catch(_){}err.value=msg;data.value=null}ld.value=false}
  const flatRows=computed(()=>{const out=[];const provs=data.value?.providers||{};for(const prov of Object.keys(provs)){const bucket=provs[prov];for(const mdl of Object.keys(bucket.models||{})){const m=bucket.models[mdl];out.push({prov,mdl,summary:m.summary});for(const d of (m.daily||[]))out.push({prov,mdl,detail:d})}out.push({prov,mdl:null,summary:bucket.summary})}return out});
  const hasData=computed(()=>{const provs=data.value?.providers||{};return Object.keys(provs).length>0});
  onMounted(()=>{loadChannels();load()});
  return{data,ld,err,f,channels,channelModels,rangePreset,onProviderChange,load,n,tok,money,ms,pct,flatRows,hasData,I,RATIO_TIP,AVG_TIP,CACHE_INC_TIP}
},template:`
<div>
  <div class="phead"><h1>用量统计</h1><p>按平台 × 模型 × 日期聚合的 Token 用量</p></div>
  <div class="control-row">
    <div class="seg">
      <button :class="{on:f.range==='1'}" @click="rangePreset('1')">今日</button>
      <button :class="{on:f.range==='7'}" @click="rangePreset('7')">近 7 天</button>
      <button :class="{on:f.range==='30'}" @click="rangePreset('30')">近 30 天</button>
      <button :class="{on:f.range==='365'}" @click="rangePreset('365')">近一年</button>
      <button :class="{on:f.range==='custom'}" @click="f.range='custom'">自定义</button>
    </div>
    <template v-if="f.range==='custom'">
      <input class="selectctl" type="date" v-model="f.start"/>
      <span class="muted">至</span>
      <input class="selectctl" type="date" v-model="f.end"/>
    </template>
    <select class="selectctl" v-model="f.provider" @change="onProviderChange">
      <option value="">全部平台</option>
      <option v-for="c in channels" :key="c.id" :value="c.id">{{c.display_name||c.id}}</option>
    </select>
    <select class="selectctl" v-model="f.model" @change="load()" :disabled="!f.provider" :title="f.provider?'':'先选择平台'">
      <option value="">{{f.provider?'全部模型':'先选平台'}}</option>
      <option v-for="m in (channelModels[f.provider]||[])" :key="m" :value="m">{{m}}</option>
    </select>
    <button class="btn s" @click="load" :disabled="ld"><span v-html="I.refresh"></span>{{ld?'查询中':'查询'}}</button>
  </div>
  <div v-if="ld" class="load"><div class="spin"></div></div>
  <div class="card card-p empty" v-else-if="err"><div class="em">!</div><p style="font-weight:600;color:var(--fg)">{{err}}</p></div>
  <template v-else-if="data">
    <div class="dash-grid thirds" style="margin-bottom:14px">
      <div class="metric"><div class="m-label">请求数</div><div class="m-value">{{n(data.totals?.requests)}}</div><div class="m-sub">所选时间范围合计</div></div>
      <div class="metric"><div class="m-label">Token 总量</div><div class="m-value">{{tok(data.totals?.total_tokens)}}</div><div class="m-sub">输入 {{tok(data.totals?.prompt_tokens)}} · 输出 {{tok(data.totals?.completion_tokens)}} · 缓存命中 {{tok(data.totals?.cache_read_tokens)}}</div></div>
      <div class="metric" :title="RATIO_TIP"><div class="m-label">缓存命中率</div><div class="m-value">{{pct(data.totals?.cache_hit_ratio)}}</div><div class="m-sub">cache_read / prompt_tokens</div></div>
    </div>
    <div class="card" v-if="hasData"><div class="table-scroll"><table>
      <thead><tr><th>平台 / 模型 / 日期</th><th>请求数</th><th>输入 Token</th><th :title="CACHE_INC_TIP">缓存命中 Token</th><th :title="RATIO_TIP">缓存命中率</th><th>输出 Token</th><th>总 Token</th><th>Credit</th><th :title="AVG_TIP">平均耗时</th></tr></thead>
      <tbody>
        <template v-for="(row,i) in flatRows" :key="i">
          <tr v-if="row.prov&&row.mdl===null&&row.summary" class="prov-row"><td style="font-weight:800">{{row.prov}} · 平台汇总</td><td>{{n(row.summary.requests)}}</td><td>{{tok(row.summary.prompt_tokens)}}</td><td>{{tok(row.summary.cache_read_tokens)}}</td><td>{{pct(row.summary.cache_hit_ratio)}}</td><td>{{tok(row.summary.completion_tokens)}}</td><td>{{tok(row.summary.total_tokens)}}</td><td>{{money(row.summary.credit)}}</td><td>{{ms(row.summary.avg_duration_ms)}}</td></tr>
          <tr v-else-if="row.mdl&&row.detail"><td class="mono" style="padding-left:32px">{{row.mdl}} · {{row.detail.date}}</td><td>{{n(row.detail.requests)}}</td><td>{{tok(row.detail.prompt_tokens)}}</td><td>{{tok(row.detail.cache_read_tokens)}}</td><td>{{pct(row.detail.cache_hit_ratio)}}</td><td>{{tok(row.detail.completion_tokens)}}</td><td>{{tok(row.detail.total_tokens)}}</td><td>{{money(row.detail.credit)}}</td><td>{{ms(row.detail.avg_duration_ms)}}</td></tr>
          <tr v-else-if="row.mdl&&row.summary" class="model-row"><td style="font-weight:600;padding-left:20px">{{row.mdl}} · 小计</td><td>{{n(row.summary.requests)}}</td><td>{{tok(row.summary.prompt_tokens)}}</td><td>{{tok(row.summary.cache_read_tokens)}}</td><td>{{pct(row.summary.cache_hit_ratio)}}</td><td>{{tok(row.summary.completion_tokens)}}</td><td>{{tok(row.summary.total_tokens)}}</td><td>{{money(row.summary.credit)}}</td><td>{{ms(row.summary.avg_duration_ms)}}</td></tr>
        </template>
      </tbody>
    </table></div></div>
    <div class="card card-p empty" v-else><div class="em">📊</div><p>所选范围内暂无用量数据</p></div>
  </template>
</div>`};
