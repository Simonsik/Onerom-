"use strict";
const $ = id => document.getElementById(id);
if (typeof L === 'undefined') $('status').textContent = 'Не удалось загрузить карту. Проверьте интернет и обновите страницу.';
else loadApplication();

async function loadApplication() {
  try {
    const response = await fetch('/api/bootstrap');
    if (!response.ok) throw new Error('Ошибка загрузки данных');
    window.DETECTOR_DATA = await response.json();
    window.MAP_DATA = {demo:false, roads:[], cameras:window.DETECTOR_DATA.sensors};
    initializeMap();
  } catch (error) {
    $('status').textContent = 'Не удалось загрузить данные сервера. Запустите python3 app.py и откройте http://localhost:8000.';
  }
}

function initializeMap() {
  const center = [54.7826, 32.0453];
  const map = L.map('map').setView(center, 13);
  const tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom:19, attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'}).addTo(map);
  map.createPane('traffic'); map.getPane('traffic').style.zIndex = 450;
  const trafficLayer = L.layerGroup().addTo(map), cameraLayer = L.layerGroup().addTo(map), routeLayer = L.layerGroup().addTo(map);
  const points = {a:null,b:null}, markers = {a:null,b:null};
  const colors = {free:'#16a36a',busy:'#e9a323',jam:'#e04444'};
  const levels = {free:'Свободно',busy:'Плотное движение',jam:'Пробка'};
  let selected = 'a', routes = [], activeRoute = 0, request = null, sequence = 0, timer;
  let data = window.MAP_DATA;
  const status = text => $('status').textContent = text;
  function update() {
    for (const key of ['a','b']) {
      $('select-'+key).classList.toggle('active', selected === key);
      $('select-'+key).setAttribute('aria-pressed', String(selected === key));
      $('coordinates-'+key).textContent = points[key] ? `${points[key].lat.toFixed(5)}, ${points[key].lng.toFixed(5)}` : 'Выберите точку на карте';
    }
    $('swap').disabled = !(points.a && points.b);
    $('reset').disabled = !(points.a || points.b);
    map.getContainer().style.cursor = selected ? 'crosshair' : '';
  }
  function select(key) {selected = key; update(); status(`Нажмите на карту: ${key === 'a' ? 'откуда едем' : 'куда едем'}?`);}
  function marker(key) {
    if (markers[key]) return markers[key].setLatLng(points[key]);
    const letter = key === 'a' ? 'А' : 'Б';
    markers[key] = L.marker(points[key], {draggable:true, title:`Точка ${letter}`,alt:`Точка ${letter}`,icon:L.divIcon({className:'',html:`<div class="map-marker ${key}">${letter}</div>`,iconSize:[36,36],iconAnchor:[18,18]})}).addTo(map);
    markers[key].on('dragend', () => {points[key] = markers[key].getLatLng(); update(); schedule();});
  }
  function invalidate() {
    clearTimeout(timer); sequence++; if(request) request.abort();
    routes = []; routeLayer.clearLayers(); $('retry').hidden = true;
    $('routes').replaceChildren();
  }
  function schedule() {
    invalidate();
    if (!points.a || !points.b) return;
    status('Ищем пути по дорожной сети…');
    timer = setTimeout(build, 350);
  }
  async function build() {
    const token = ++sequence;
    const controller = new AbortController(); request = controller;
    const timeout = setTimeout(() => controller.abort(), 20000);
    $('retry').hidden = true;
    try {
      const coords = ['a','b'].map(k => `${points[k].lng},${points[k].lat}`).join(';');
      const response = await fetch(`/api/route?points=${encodeURIComponent(coords)}`, {signal:controller.signal});
      if (!response.ok) throw new Error('Сервис маршрутов недоступен');
      const result = await response.json();
      if(token !== sequence) return;
      if(result.code !== 'Ok' || !result.routes?.length) throw new Error('Между точками не найден автомобильный путь');
      if(result.waypoints.some(w => w.distance > 1000)) throw new Error('Точка слишком далеко от автомобильной дороги. Выберите точку ближе к дороге');
      routes = result.routes.sort((a,b) => a.distance-b.distance); activeRoute = 0;
      drawRoutes(true);
      status(`Найдено вариантов: ${routes.length}. Выбран самый короткий из них.${result.waypoints.some(w=>w.distance>50) ? ' Начало и конец привязаны к ближайшим дорогам.' : ''}`);
    } catch(error) {
      if(token !== sequence) return;
      status(error.name === 'AbortError' ? 'Сервис не ответил вовремя. Попробуйте ещё раз.' : `${error.message}. Попробуйте изменить точки или повторить запрос.`);
      $('retry').hidden = false;
    } finally {clearTimeout(timeout); if(request === controller) request = null;}
  }
  function drawRoutes(fit = false) {
    routeLayer.clearLayers(); $('routes').replaceChildren();
    // Сначала альтернативы, затем выбранный путь поверх них.
    const order = routes.map((_,i)=>i).filter(i=>i!==activeRoute).concat(activeRoute);
    for(const i of order) {
      const line = L.geoJSON(routes[i].geometry,{style:{color:i===activeRoute?'#2563eb':'#7a8ba4',weight:i===activeRoute?7:5,opacity:i===activeRoute?0.95:0.6}}).addTo(routeLayer);
      line.on('click', e => {L.DomEvent.stopPropagation(e); activeRoute = i; drawRoutes();});
    }
    routes.forEach((route,i) => {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'route-card'+(i===activeRoute?' chosen':'');
      button.setAttribute('aria-pressed', String(i===activeRoute));
      const title = document.createElement('strong'); title.textContent = i===0?'Короткий среди вариантов':`Альтернатива ${i}`;
      const details = document.createElement('span'); details.textContent = `${(route.distance/1000).toFixed(1)} км · ≈ ${Math.max(1,Math.round(route.duration/60))} мин`;
      button.append(title,details); button.onclick = () => {activeRoute=i;drawRoutes(true);}; $('routes').append(button);
    });
    if(fit) map.fitBounds(L.geoJSON(routes[activeRoute].geometry).getBounds(),{padding:[45,45],maxZoom:16});
  }
  function textPopup(title, detail) {
    const node = document.createElement('div'), strong = document.createElement('strong'), p = document.createElement('p');
    strong.textContent=title; p.textContent=detail; node.append(strong,p);return node;
  }
  function renderData() {
    trafficLayer.clearLayers(); cameraLayer.clearLayers(); $('roads').replaceChildren();
    $('data-badge').textContent = data.demo ? 'ДЕМО':'ЗАГРУЖЕНО';
    $('data-note').textContent = data.demo ? 'Условные участки и камеры для демонстрации. Геометрия не привязана к дорогам; это не текущие пробки.' : 'Координаты детекторов из таблицы. Дорожные участки пока не предоставлены. При загрузке JSON геометрия определяется вашим файлом.';
    data.roads.forEach(road => {
      const line=L.polyline(road.coordinates,{pane:'traffic',color:colors[road.level],weight:6,opacity:0.85}).addTo(trafficLayer).bindPopup(textPopup(road.name,levels[road.level]));
      const row=document.createElement('button');row.type='button';row.className='road-row';
      const dot=document.createElement('i');dot.className='dot';dot.style.background=colors[road.level];
      const name=document.createElement('span');name.textContent=road.name;
      const label=document.createElement('small');label.textContent=levels[road.level];
      row.append(dot,name,label);row.onclick=()=>{if(!$('show-traffic').checked){$('show-traffic').checked=true;map.addLayer(trafficLayer);}map.fitBounds(line.getBounds(),{padding:[40,40],maxZoom:16});line.openPopup();};$('roads').append(row);
    });
    if (!window.DETECTOR_DATA || data !== window.MAP_DATA) data.cameras.forEach(camera => L.marker(camera.coordinates,{title:camera.name,alt:camera.name,icon:L.divIcon({className:'',html:'<div class="camera-marker">●</div>',iconSize:[26,26],iconAnchor:[13,13]})}).addTo(cameraLayer).bindPopup(textPopup(camera.name,`${camera.coordinates.join(', ')}${data.demo?' · демонстрационная камера':''}`)));
  }
  const validCoord = c => Array.isArray(c)&&c.length===2&&c.every(Number.isFinite)&&Math.abs(c[0])<=90&&Math.abs(c[1])<=180;
  function validate(value) {
    if(!value || typeof value.demo!=='boolean' || !Array.isArray(value.roads) || !Array.isArray(value.cameras)) throw new Error('Нужны demo (true/false), roads и cameras');
    if(value.roads.length>500 || value.cameras.length>2000) throw new Error('Слишком много объектов');
    for(const r of value.roads) if(!r || typeof r.name!=='string' || !Object.hasOwn(colors,r.level) || !Array.isArray(r.coordinates) || r.coordinates.length<2 || r.coordinates.length>10000 || !r.coordinates.every(validCoord)) throw new Error('Проверьте название, level и координаты дорог');
    for(const c of value.cameras) if(!c || typeof c.name!=='string'||!validCoord(c.coordinates)) throw new Error('Проверьте названия и координаты камер');
    return value;
  }
  $('import-data').onchange = async event => {
    try {const file=event.target.files[0];if(!file)return;if(file.size>5000000)throw new Error('Файл должен быть меньше 5 МБ'); const parsed=validate(JSON.parse(await file.text()));data=parsed;renderData();$('data-status').textContent=`Загружено: ${data.roads.length} участков, ${data.cameras.length} камер. Данные действуют до перезагрузки страницы.`;}
    catch(error){$('data-status').textContent=`Не удалось загрузить: ${error.message}`;}
    finally{event.target.value='';}
  };
  for(const [id,layer] of [['show-traffic',trafficLayer],['show-cameras',cameraLayer]]) $(id).onchange=e=>e.target.checked?map.addLayer(layer):map.removeLayer(layer);
  $('select-a').onclick=()=>select('a');$('select-b').onclick=()=>select('b');
  map.on('click',e=>{if(!selected)return;const key=selected;points[key]={lat:e.latlng.lat,lng:e.latlng.lng};marker(key);if(!points.a)select('a');else if(!points.b)select('b');else{selected=null;update();schedule();}});
  $('swap').onclick=()=>{if(!points.a||!points.b)return;[points.a,points.b]=[points.b,points.a];marker('a');marker('b');selected=null;update();schedule();};
  $('reset').onclick=()=>{invalidate();for(const key of ['a','b']){if(markers[key])map.removeLayer(markers[key]);markers[key]=null;points[key]=null;} $('routes').textContent='Выберите А и Б. Построим автомобильные маршруты по дорогам.';select('a');};
  $('center').onclick=()=>map.setView(center,13);$('retry').onclick=()=>schedule();
  let warned=false;tiles.on('tileerror',()=>{if(!warned){warned=true;status('Часть карты не загрузилась. Проверьте подключение к интернету.');}});
  window.getRoutePoints=()=>({start:points.a?{...points.a}:null,end:points.b?{...points.b}:null});
  let detectorRequest = 0;
  async function renderDetectors() {
    const token = ++detectorRequest;
    if (!window.DETECTOR_DATA || data !== window.MAP_DATA) return;
    cameraLayer.clearLayers(); $('detector-list').replaceChildren();
    const dataset = window.DETECTOR_DATA.datasets[Number($('dataset').value)];
    const from = $('time-from').value, to = $('time-to').value;
    if (!from || !to || from > to) { $('detector-summary').textContent = 'Укажите интервал внутри одного дня: начало не позже конца.'; return; }
    let result;
    $('detector-summary').textContent = 'Считаем проезды…';
    try {
      const params = new URLSearchParams({dataset:$('dataset').value, from, to});
      const response = await fetch(`/api/detectors?${params}`);
      result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Ошибка сервера');
      if (token !== detectorRequest || data !== window.MAP_DATA) return;
    } catch (error) {
      if (token === detectorRequest) $('detector-summary').textContent = `Не удалось получить статистику: ${error.message}`;
      return;
    }
    const counts = new Map(Object.entries(result.counts));
    $('detector-summary').textContent = `${result.total} проездов · ${result.vehicles} машин · замечаний импорта: ${dataset.issues.length}`;
    const sensors = [...window.DETECTOR_DATA.sensors].sort((a,b)=>(counts.get(b.name)||0)-(counts.get(a.name)||0));
    for (const sensor of sensors) {
      const count = counts.get(sensor.name)||0;
      const icon = L.divIcon({className:'',html:`<div class="detector-marker"><b>${sensor.name}</b><span>${count}</span></div>`,iconSize:[48,42],iconAnchor:[24,21]});
      const marker = L.marker(sensor.coordinates,{icon,title:`${sensor.name}: ${count} проездов`,alt:sensor.name}).addTo(cameraLayer).bindPopup(textPopup(sensor.name, `${count} проездов · ${from}–${to} · ${dataset.name} · 15.10.2025`));
      const button = document.createElement('button');button.type='button';button.className='road-row';button.textContent=`${sensor.name} · ${count} проездов`;
      button.onclick=()=>{if(!$('show-cameras').checked){$('show-cameras').checked=true;map.addLayer(cameraLayer);}map.setView(sensor.coordinates,16);marker.openPopup();};$('detector-list').append(button);
    }
  }
  for (const id of ['dataset','time-from','time-to']) $(id).addEventListener('change',renderDetectors);
  renderData();renderDetectors();select('a');
}
