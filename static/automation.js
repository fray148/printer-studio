'use strict';
(() => {
const $=id=>document.getElementById(id), csrf=document.querySelector('meta[name=csrf-token]').content;
const botPage=document.body.dataset.tab==='bot';
const statusNames={recording:'Идёт запись',queued:'Ожидает сборки',encoding:'Сборка видео',ready:'Готово',failed:'Нужна проверка'};
let initialized=false,busy=false,timer;
function notice(message,error=false){$('notice').hidden=false;$('notice').textContent=message;$('notice').classList.toggle('error',error)}
async function api(path,method='GET',body){
 const response=await fetch(path,{method,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:body===undefined?undefined:JSON.stringify(body)});
 if(!(response.headers.get('content-type')||'').includes('application/json'))throw Error('Сессия недоступна. Войди в панель и обнови страницу.');
 const data=await response.json();if(!response.ok)throw Error(data.error||'Не удалось выполнить запрос.');return data;
}
async function action(fn){if(busy)return;busy=true;try{await fn();await refresh()}catch(e){notice(e.message,true)}finally{busy=false}}
function archive(records){
 const existing=new Map([...$('archive').children].map(el=>[el.dataset.id,el]));
 $('archive-empty').hidden=records.length>0;$('archive-count').textContent=`${records.length} записей`;
 let position=0;
 for(const record of records){
  const index=position++;
  let el=existing.get(record.id);const signature=JSON.stringify(record);existing.delete(record.id);
  if(el&&el.dataset.signature===signature)continue;
  const card=$('recording-template').content.firstElementChild.cloneNode(true);card.dataset.id=record.id;card.dataset.signature=signature;
  card.querySelector('.rec-status').textContent=statusNames[record.status]||record.status;card.querySelector('.rec-status').classList.add(record.status);
  card.querySelector('.rec-date').textContent=new Date(record.created*1000).toLocaleString('ru-RU');
  card.querySelector('.rec-label').textContent=record.label;
  card.querySelector('.rec-details').textContent=`${record.frames} кадров · интервал ${record.interval} с · ${record.fps} FPS${record.size?' · '+(record.size/1024/1024).toFixed(1)+' МБ':''}`;
  card.querySelector('.rec-error').textContent=record.error||'';
  const ready=record.status==='ready', working=['recording','queued','encoding'].includes(record.status),base='/api/automation/recordings/'+record.id;
  const video=card.querySelector('video');video.hidden=!ready;if(ready){video.src=base+'/video';video.poster=base+'/poster'}
  const download=card.querySelector('.rec-download');download.hidden=!ready;download.href=base+'/video?download=1';
  const retry=card.querySelector('.rec-retry');retry.hidden=record.status!=='failed';retry.onclick=()=>action(()=>api(base+'/retry','POST'));
  const del=card.querySelector('.rec-delete');del.disabled=working;del.onclick=()=>{if(confirm('Удалить «'+record.label+'», видео и все исходные кадры?'))action(()=>api(base,'DELETE'))};
  const files=card.querySelector('.files');files.hidden=working;
  card.querySelector('.rec-frames').href=base+'/frames.zip';
  card.querySelector('.rec-info').href=base+'/info';
  card.querySelector('.rec-rename').onclick=()=>{const name=prompt('Название записи',record.label);if(name!==null)action(()=>api(base+'/rename','POST',{label:name}))};
  if(el)el.replaceWith(card);else $('archive').insertBefore(card,$('archive').children[index]||null);
 }
 existing.forEach(el=>el.remove());
}
async function refresh(){
 try{
  const d=await api('/api/automation'),s=d.settings,current=d.recordings.find(r=>r.id===d.active);
  $('capture-state').textContent=current?'Идёт запись':s.auto_record?'Ожидание печати':'Выключен';
  $('capture-count').replaceChildren(document.createTextNode(String(current?.frames||0)+' '));const unit=document.createElement('span');unit.className='unit';unit.textContent='кадров';$('capture-count').append(unit);
  $('capture-label').textContent=current?.label||'Запись не запущена';$('record-start').disabled=!!current;$('record-stop').disabled=!current;
  $('bot-state').textContent=s.telegram_enabled?(s.bot_error?'Ошибка связи':s.bot_name?'Подключён':'Подключение…'):'Выключен';
  $('bot-name').textContent=s.bot_name?'@'+s.bot_name:s.token_configured?'Токен сохранён':'Не настроен';
  $('bot-error').textContent=s.bot_error||'Бот отвечает только разрешённому chat_id.';$('telegram-test').disabled=!s.telegram_enabled;
  if(!initialized){
   for(const key of ['auto_record','telegram_enabled','send_video'])$(key).checked=s[key];
   for(const key of ['interval','fps','retention_days','storage_gb','min_free_gb','notify_progress','chat_id'])$(key).value=s[key];
   initialized=true;
   $('save-settings').disabled=false;
  }
  $('bot_token').placeholder=s.token_configured?'Токен сохранён — оставь пустым, чтобы не менять':'Токен от BotFather';
  if(s.last_error)notice(s.last_error,true);
  if(!botPage)archive(d.recordings);
 }catch(e){notice(e.message,true)}
}
$('settings-form').onsubmit=e=>{e.preventDefault();action(async()=>{
 const values={};
 if(botPage){
  for(const key of ['telegram_enabled','send_video','clear_token'])values[key]=$(key).checked;
  values.notify_progress=Number($('notify_progress').value);
  values.chat_id=$('chat_id').value;values.bot_token=$('bot_token').value;
 }else{
  values.auto_record=$('auto_record').checked;
  for(const key of ['interval','fps','retention_days','storage_gb','min_free_gb'])values[key]=Number($(key).value);
 }
 await api('/api/automation/settings','POST',values);$('bot_token').value='';$('clear_token').checked=false;notice('Настройки сохранены.');
})};
$('record-start').onclick=()=>action(()=>api('/api/automation/start','POST',{label:$('record-label').value}));
$('record-stop').onclick=()=>action(()=>api('/api/automation/stop','POST'));
$('telegram-test').onclick=()=>action(async()=>{await api('/api/automation/telegram-test','POST');notice('Тестовое сообщение поставлено в очередь.');});
refresh();timer=setInterval(()=>{if(!document.hidden&&!busy)refresh()},5000);
})();
