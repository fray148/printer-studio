"use strict";
(()=>{
const $=id=>document.getElementById(id),names={print:"Печать",pause:"Пауза",tool:"Инструмент",state:"Состояние",error:"Ошибка",connection:"Связь"};
let oldest=null,busy=false,revision=0;
async function load(append=false){
 if(busy)return;busy=true;const current=revision;
 $("event-status").textContent="Загрузка…";
 try{
  const query=new URLSearchParams({kind:$("event-kind").value});if(append&&oldest)query.set("before",oldest);
  const response=await fetch("/api/automation/events?"+query);if(!response.ok)throw Error();
  const {events}=await response.json();if(current!==revision)return;
  if(!append)$("events").replaceChildren();
  for(const event of events){
   const row=document.createElement("tr");row.dataset.kind=event.kind;
   for(const value of [new Date(event.created*1000).toLocaleString("ru-RU"),names[event.kind]||event.kind,event.message,event.file||"—"]){const cell=document.createElement("td");cell.textContent=value;row.append(cell);}
   const details=event.details||{};
   if(event.kind==="tool"&&details.from_tool&&details.to_tool){
    const cell=row.children[2];cell.replaceChildren(document.createTextNode("Смена инструмента: "));
    for(const [i,tool] of [details.from_tool,details.to_tool].entries()){
     if(i)cell.append(document.createTextNode(" → "));
     const label=document.createElement("span");label.className="event-tool";label.append(document.createTextNode(String(tool.id)+" ("));
     if(/^#[0-9a-f]{6}$/i.test(tool.color||"")){
      const swatch=document.createElement("span");swatch.className="event-color";swatch.style.backgroundColor=tool.color;
      swatch.title=tool.color;swatch.setAttribute("role","img");swatch.setAttribute("aria-label","Цвет "+tool.color);label.append(swatch);
     }else label.append(document.createTextNode("цвет неизвестен"));
     label.append(document.createTextNode(")"));cell.append(label);
    }
    if(details.color_source==="current_slots"){
     const note=document.createElement("small");note.className="sub event-color-note";note.textContent="цвета восстановлены";
     note.title="По цветам слотов на момент обновления журнала; исторические цвета не сохранялись.";cell.append(note);
    }
   }
   $("events").append(row);
  }
  if(events.length)oldest=events[events.length-1].id;else if(!append)oldest=null;
  $("more").hidden=events.length<100;$("event-empty").hidden=$("events").children.length>0;
  $("event-status").textContent="Обновлено: "+new Date().toLocaleTimeString("ru-RU");
 }catch(e){$("event-status").textContent="Не удалось загрузить события. Повтори обновление.";}
 finally{busy=false;if(current!==revision)load();}
}
$("event-kind").onchange=()=>{revision++;oldest=null;load();};$("refresh").onclick=()=>load();$("more").onclick=()=>load(true);
setInterval(()=>{if(!document.hidden&&$("events").children.length<=100)load();},10000);load();
})();
