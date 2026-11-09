// Caller substitutes only JSON string literals for the two absolute paths.
var runtimeStage = 'architecture_check';
if (Process.arch !== 'x64') throw new Error('This experiment is x64 only.');
runtimeStage = 'resolve_existing_clr';
Process.getModuleByName('clr.dll'); // Never load/start a new CLR.
const mod = Process.getModuleByName('mscoree.dll');
function guid(text) {
  const parts = text.split('-'), memory = Memory.alloc(16);
  memory.writeU32(parseInt(parts[0],16)); memory.add(4).writeU16(parseInt(parts[1],16));
  memory.add(6).writeU16(parseInt(parts[2],16));
  const tail = parts[3] + parts[4];
  for (let i=0;i<8;i++) memory.add(8+i).writeU8(parseInt(tail.slice(i*2,i*2+2),16));
  return memory;
}
function method(object,index,types) {
  return new NativeFunction(object.readPointer().add(index*Process.pointerSize).readPointer(),'int',types);
}
function check(value,stage) { if(value<0) throw new Error(stage+': HRESULT '+(value>>>0).toString(16)); }
const output = Memory.alloc(Process.pointerSize), infoOut = Memory.alloc(Process.pointerSize), hostOut = Memory.alloc(Process.pointerSize);
const create = new NativeFunction(mod.getExportByName('CLRCreateInstance'),'int',['pointer','pointer','pointer']);
let meta=ptr(0),info=ptr(0),host=ptr(0);
try {
  runtimeStage = 'CLRCreateInstance';
  check(create(guid('9280188d-0e8e-4867-b30c-7fa83884e8de'),guid('d332db9e-b9b3-4125-8207-a14884f53216'),output),'CLRCreateInstance');
  meta=output.readPointer();
  runtimeStage = 'GetRuntime';
  check(method(meta,3,['pointer','pointer','pointer','pointer'])(meta,Memory.allocUtf16String('v4.0.30319'),guid('bd39d1d2-ba2f-486a-89b0-b4b0cb466891'),infoOut),'GetRuntime');
  info=infoOut.readPointer();
  const started=Memory.alloc(4),flags=Memory.alloc(4);
  runtimeStage = 'IsStarted';
  check(method(info,14,['pointer','pointer','pointer'])(info,started,flags),'IsStarted');
  if(started.readU32()===0) throw new Error('The existing CLR is not started.');
  runtimeStage = 'GetInterface';
  check(method(info,9,['pointer','pointer','pointer','pointer'])(info,guid('90f1a06e-7712-4762-86b5-7a5eba6bdb02'),guid('90f1a06c-7712-4762-86b5-7a5eba6bdb02'),hostOut),'GetInterface');
  host=hostOut.readPointer();
  const result=Memory.alloc(4);
  runtimeStage = 'ExecuteInDefaultAppDomain';
  const hresult=method(host,11,['pointer','pointer','pointer','pointer','pointer','pointer'])(
    host,Memory.allocUtf16String(ASSEMBLY_PATH),Memory.allocUtf16String('SysmacMcp.Runtime.Entry'),
    Memory.allocUtf16String('Invoke'),Memory.allocUtf16String(REQUEST_JSON),result);
  check(hresult,'ExecuteInDefaultAppDomain');
  runtimeStage = 'read_managed_result';
  send({probe:'managed-runtime',pid:Process.id,hresult:hresult,managed_return:result.readU32(),runtime_started_before:true});
} finally {
  for(const object of [host,info,meta]) if(!object.isNull()) method(object,2,['pointer'])(object);
}
