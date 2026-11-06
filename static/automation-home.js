'use strict';
(async function(){
 async function update(){try{
  const response=await fetch('/api/automation');if(!response.ok)return;const d=await response.json(),s=d.settings,r=d.recordings.find(x=>x.id===d.active),$=id=>document.getElementById(id);
  $('home-capture-state').textContent=r?'Идёт запись':s.auto_record?'Ожидание печати':'Выключен';
  $('home-frame-count').textContent=r?.frames||0;$('home-capture-label').textContent=r?.label||`${d.recordings.length} записей в архиве · кадр каждые ${s.interval} с`;
  $('home-bot-state').textContent=s.telegram_enabled?(s.bot_error?'Ошибка связи':s.bot_name?'Подключён':'Подключение…'):'Выключен';
  $('home-bot-name').textContent=s.bot_name?'@'+s.bot_name:s.token_configured?'Токен сохранён':'Не настроен';
 }catch(e){/* Existing printer dashboard remains usable if automation is unavailable. */}}
 await update();setInterval(()=>{if(!document.hidden)update()},10000);
})();
