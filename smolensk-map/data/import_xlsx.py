"""Импорт исходных XLSX без сторонних библиотек: python3 data/import_xlsx.py."""
import csv, json, zipfile, collections
import xml.etree.ElementTree as E
from pathlib import Path
from datetime import datetime, timedelta
ROOT=Path(__file__).resolve().parent.parent
NS={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
def rows(path):
 with zipfile.ZipFile(path) as z:
  strings=[]
  if 'xl/sharedStrings.xml' in z.namelist():
   strings=[''.join(t.text or '' for t in s.findall('.//s:t',NS)) for s in E.fromstring(z.read('xl/sharedStrings.xml')).findall('s:si',NS)]
  out=[]
  for row in E.fromstring(z.read('xl/worksheets/sheet1.xml')).findall('.//s:sheetData/s:row',NS):
   vals={}
   for c in row.findall('s:c',NS):
    v=c.find('s:v',NS);s=v.text if v is not None else ''.join(t.text or '' for t in c.findall('.//s:t',NS))
    if c.get('t')=='s':s=strings[int(s)]
    vals[''.join(x for x in c.get('r') if x.isalpha())]=s
   out.append(vals)
  return out
def load_workbooks(source):
 source=Path(source)
 sensors=[{'name':r['A'].strip(),'coordinates':[float(r['B']),float(r['C'])]} for r in rows(source/'Таблица координат датчиков.xlsx')[1:]]
 known={s['name'] for s in sensors}; datasets=[]
 for name in ['Выборка 1.xlsx','Выборка 2.xlsx']:
  records=[]; issues=[]; seen=set(); duplicates=0
  raw=rows(source/name)[1:]
  for number,r in enumerate(raw,2):
   try:
    values=next(csv.reader([r['A']])) if len(r)==1 else [r.get(k,'') for k in 'ABCD']
    if len(values)!=4:raise ValueError('Ожидается четыре поля')
    detector,time,plate,speed=[v.strip() for v in values]
    if detector not in known:raise ValueError('Неизвестный детектор: '+detector)
    try: dt=datetime.fromisoformat(time)
    except ValueError:dt=datetime(1899,12,30)+timedelta(seconds=round(float(time)*86400))
    speed=float(speed.replace(',','.'))
    if not plate:raise ValueError('Пустой номер')
    if not 0<=speed<=300:raise ValueError('Некорректная скорость')
    record=[detector,dt.isoformat(sep=' '),plate,speed]
    key=tuple(record)
    if key in seen:duplicates+=1;issues.append({'row':number,'reason':'Точный дубликат (оставлен в исходных данных, исключён из счётчиков)'});continue
    seen.add(key);records.append(record)
   except (ValueError,KeyError,TypeError,OverflowError) as e:issues.append({'row':number,'reason':str(e),'raw':r})
  records.sort(key=lambda r:r[1]); hours=collections.Counter(r[1][11:13] for r in records)
  datasets.append({'name':name,'rawCount':len(raw),'records':records,'issues':issues,'duplicates':duplicates,'vehicles':len({r[2] for r in records}),'hours':dict(sorted(hours.items()))})
 return {"sensors": sensors, "datasets": datasets}

if __name__ == "__main__":
 print(json.dumps(load_workbooks(ROOT / "data"), ensure_ascii=False, indent=2))
