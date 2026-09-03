import {api,apiErr,n,tok,money,ms,fmtSec as fmt} from '../api.js';
import {I} from '../icons.js';
const{ref,reactive,computed,onMounted}=Vue;

export default {props:['token'],setup(p){
  const l=ref([]),meta=ref({total:0,limit:100,offset:0,models:[]}),ld=ref(true),err=ref(''),open=ref(null),copied=ref(false);
  const f=reactive({q:'',status:'all',account_id:'',api_key_id:'',model:'',limit:100,offset:0});
  function qs(){const u=new URLSearchParams();['q','status','account_id','api_key_id','model','limit','offset'].forEach(k=>{if(f[k]!==''&&f[k]!==null&&f[k]!==undefined)u.set(k,f[k])});return u.toString()}
  async function load(reset=false){if(reset)f.offset=0;ld.value=true;err.value='';try{const r=await api.get('/admin/logs/search?'+qs(),p.token);l.value=r.items||[];meta.value=r}catch(e){err.value=apiErr(e,'日志加载失败')}ld.value=false}
  function statusClass(x){return x.status_code>=200&&x.status_code<300&&x.finish_reason!=='error'&&x.finish_reason!=='content_filter'?'ok':(x.finish_reason==='content_filter'?'warn':'err')}
  function statusText(x){return x.finish_reason||x.status_code||'-'}
  function prev(){f.offset=Math.max(0,Number(f.offset||0)-Number(f.limit||100));load()}
  function next(){if(Number(f.offset||0)+Number(f.limit||100)>=Number(meta.value.total||0))return;f.offset=Number(f.offset||0)+Number(f.limit||100);load()}
  function copyErr(x){navigator.clipboard.writeText(x.error_msg||'');copied.value=true;setTimeout(()=>copied.value=false,1200)}
  const pageText=computed(()=>{const total=Number(meta.value.total||0);if(!total)return '0 / 0';return (Number(f.offset||0)+1)+'-'+Math.min(Number(f.offset||0)+Number(f.limit||100),total)+' / '+total})
  onMounted(load);return{l,meta,ld,err,open,copied,f,load,fmt,n,tok,money,ms,statusClass,statusText,prev,next,copyErr,pageText,I}
},template:`
<div>
  <div class="phead"><h1>请求日志</h1><p>请求流水、错误排查和用量追踪</p></div>
  <div class="control-row">
    <input class="searchbox" v-model="f.q" @keydown.enter="load(true)" placeholder="搜索 Key / 账号 / 模型 / Client / 错误"/>
    <div class="seg"><button :class="{on:f.status==='all'}" @click="f.status='all';load(true)">全部</button><button :class="{on:f.status==='success'}" @click="f.status='success';load(true)">成功</button><button :class="{on:f.status==='error'}" @click="f.status='error';load(true)">错误</button><button :class="{on:f.status==='filtered'}" @click="f.status='filtered';load(true)">过滤</button></div>
    <select class="selectctl" v-model="f.model" @change="load(true)"><option value="">全部模型</option><option v-for="m in meta.models" :key="m" :value="m">{{m}}</option></select>
    <select class="selectctl" v-model.number="f.limit" @change="load(true)"><option :value="50">50 条</option><option :value="100">100 条</option><option :value="200">200 条</option><option :value="500">500 条</option></select>
    <button class="btn s" @click="load(true)" :disabled="ld"><span v-html="I.refresh"></span>{{ld?'查询中':'查询'}}</button>
  </div>
  <div v-if="ld" class="load"><div class="spin"></div></div>
  <div class="card card-p empty" v-else-if="err"><div class="em">!</div><p>{{err}}</p></div>
  <div class="card" v-else-if="l.length"><table><thead><tr><th>时间</th><th>Key</th><th>账号</th><th>模型</th><th>思考</th><th>Client</th><th>流</th><th>Prompt</th><th>Completion</th><th>Token</th><th>Credit</th><th>耗时</th><th>状态</th><th></th></tr></thead><tbody>
    <template v-for="x in l" :key="x.id"><tr><td class="mono">{{fmt(x.created_at)}}</td><td>{{x.api_key_name||'-'}}</td><td>{{x.account_name||'-'}}</td><td><code>{{x.model}}</code></td><td><code :title="x.reasoning_effort?('思考档位：'+x.reasoning_effort):'未设置（跟随上游默认）'">{{x.reasoning_effort||'-'}}</code></td><td :title="x.client&&x.client_version?(x.client+' '+x.client_version):(x.client||x.client_version||'-')"><code>{{x.client||'-'}}</code></td><td>{{x.stream?'Stream':'-'}}</td><td>{{tok(x.prompt_tokens)}}</td><td>{{tok(x.completion_tokens)}}</td><td>{{tok(x.total_tokens)}}</td><td>{{money(x.credit)}}</td><td>{{ms(x.duration_ms)}}</td><td><span class="badge" :class="statusClass(x)">{{statusText(x)}}</span></td><td><button class="btn s" @click="open=open===x.id?null:x.id">{{open===x.id?'收起':'详情'}}</button></td></tr>
      <tr class="detail-row" v-if="open===x.id"><td colspan="14"><div class="detail-grid"><div><div class="detail-k">Log ID</div><div class="detail-v">{{x.id}}</div></div><div><div class="detail-k">API Key ID</div><div class="detail-v">{{x.api_key_id||'-'}}</div></div><div><div class="detail-k">Account ID</div><div class="detail-v">{{x.account_id||'-'}}</div></div><div><div class="detail-k">HTTP</div><div class="detail-v">{{x.status_code||'-'}}</div></div><div><div class="detail-k">Client</div><div class="detail-v">{{x.client||'-'}}</div></div><div><div class="detail-k">Client 版本</div><div class="detail-v">{{x.client_version||'-'}}</div></div></div><div v-if="x.error_msg"><div class="status-line" style="margin-bottom:8px"><strong style="font-size:12px">错误信息</strong><button class="btn s" @click="copyErr(x)">{{copied?'已复制':'复制错误'}}</button></div><div class="errbox">{{x.error_msg}}</div></div><div class="hint" v-else>该请求没有错误信息。</div></td></tr></template>
  </tbody></table><div class="pager"><button class="btn s" @click="prev" :disabled="f.offset<=0||ld">上一页</button><span class="tag">{{pageText}}</span><button class="btn s" @click="next" :disabled="Number(f.offset)+Number(f.limit)>=Number(meta.total)||ld">下一页</button></div></div>
  <div class="card card-p empty" v-else><div class="em">📋</div><p>暂无记录</p></div>
</div>`};
