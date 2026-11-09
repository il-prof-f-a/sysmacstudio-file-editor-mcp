import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {pathToFileURL} from 'node:url';

const LIMIT = 1024 * 1024;
const localPath = value => typeof value === 'string' && /^[A-Za-z]:[\\/]/.test(value)
    && !value.slice(2).includes(':');
const guid = value => typeof value === 'string'
    && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(value)
    && value !== '00000000-0000-0000-0000-000000000000';
const started = value => typeof value === 'string'
    && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{7}Z$/.test(value);

export function validateRequest(request) {
    if (!request || typeof request !== 'object' || Array.isArray(request)) throw Error('Invalid request.');
    const fields = ['operation', 'process_id', 'process_started_utc', 'solution_id',
        'controller_id', 'storage_folder', 'archive_output', 'output', 'progress_output', 'timeout_ms', 'offset', 'limit'];
    if (Object.keys(request).some(key => !fields.includes(key))) throw Error('Unknown request field.');
    if (!['status', 'rebuild', 'list-projects', 'open-existing', 'close-active', 'export'].includes(request.operation)) throw Error('Unsupported runtime operation.');
    if (!Number.isInteger(request.process_id) || request.process_id < 1 || request.process_id > 2147483647)
        throw Error('Invalid Sysmac PID.');
    if (!Number.isInteger(request.timeout_ms) || request.timeout_ms < 1 || request.timeout_ms > 600000)
        throw Error('Invalid runtime deadline.');
    if (!localPath(request.output) || path.extname(request.output) !== '.json') throw Error('Invalid output path.');
    const hasProgress = Object.prototype.hasOwnProperty.call(request, 'progress_output');
    if (['status', 'export'].includes(request.operation) && !hasProgress)
        throw Error('Missing runtime progress path.');
    if (hasProgress && ['status', 'export'].includes(request.operation)) {
        if (!localPath(request.progress_output) || path.extname(request.progress_output).toLowerCase() !== '.jsonl')
            throw Error('Invalid runtime progress path.');
        const outputPath = path.resolve(request.output), progressPath = path.resolve(request.progress_output);
        if (path.dirname(outputPath).toLowerCase() !== path.dirname(progressPath).toLowerCase()
            || outputPath.toLowerCase() === progressPath.toLowerCase())
            throw Error('Runtime progress must be a separate file beside the response.');
    } else if (hasProgress) throw Error('Progress output is only valid for status or export.');
    if (request.operation === 'rebuild' && (!started(request.process_started_utc)
        || !guid(request.solution_id) || !guid(request.controller_id))) throw Error('Missing expected context.');
    if (request.operation === 'open-existing' && (!started(request.process_started_utc) || !guid(request.solution_id)))
        throw Error('Missing expected persisted project context.');
    if (request.operation === 'close-active' && (!started(request.process_started_utc) || !guid(request.solution_id)))
        throw Error('Missing expected solution close context.');
    if (request.operation === 'export') {
        if (!started(request.process_started_utc) || !guid(request.solution_id))
            throw Error('Missing expected persisted solution context.');
        if (!localPath(request.storage_folder) || !localPath(request.archive_output)
            || path.extname(request.archive_output).toLowerCase() !== '.smc2')
            throw Error('Invalid guarded export paths.');
        const storageFolder = path.resolve(request.storage_folder);
        const archiveOutput = path.resolve(request.archive_output);
        if (path.parse(storageFolder).root.toLowerCase() !== path.parse(archiveOutput).root.toLowerCase())
            throw Error('Export paths must use the same local drive.');
        let storageInfo, outputParentInfo;
        try {
            storageInfo = fs.statSync(storageFolder);
            outputParentInfo = fs.statSync(path.dirname(archiveOutput));
        } catch {
            throw Error('Export storage folder and output parent must exist.');
        }
        if (!storageInfo.isDirectory() || !outputParentInfo.isDirectory() || fs.existsSync(archiveOutput))
            throw Error('Export output must be new and its folders must exist.');
        const relativeOutput = path.relative(storageFolder, archiveOutput);
        if (!relativeOutput || (!relativeOutput.startsWith('..' + path.sep)
            && relativeOutput !== '..' && !path.isAbsolute(relativeOutput)))
            throw Error('Export output must be outside the active persistence folder.');
    }
    if (request.operation === 'list-projects' && (!Number.isInteger(request.offset ?? 0) || (request.offset ?? 0) < 0
        || (request.offset ?? 0) > 2147483647 || !Number.isInteger(request.limit ?? 100)
        || (request.limit ?? 100) < 1 || (request.limit ?? 100) > 200)) throw Error('Invalid inventory page.');
    return request;
}

function readBounded(file, limit = LIMIT) {
    if (!localPath(file) || !fs.statSync(file).isFile() || fs.statSync(file).size > limit)
        throw Error('Invalid or oversized local input.');
    const value = fs.readFileSync(file);
    if (value.length > limit) throw Error('Input changed beyond the size limit.');
    return value;
}
function digest(bytes) { return crypto.createHash('sha256').update(bytes).digest('hex'); }
function verifiedFile(profile, name) {
    const value = profile[name];
    if (!value || !localPath(value.path) || !/^[0-9a-f]{64}$/.test(value.sha256)
        || digest(fs.readFileSync(value.path)) !== value.sha256) throw Error('Runtime component hash differs: ' + name);
    return value.path;
}

export function makeSource(template, assembly, request) {
    if (!template.includes('ASSEMBLY_PATH') || !template.includes('REQUEST_JSON')) throw Error('Invalid CLR template.');
    const values = {ASSEMBLY_PATH:JSON.stringify(assembly), REQUEST_JSON:JSON.stringify(JSON.stringify(request))};
    const source = template.replace(/\b(ASSEMBLY_PATH|REQUEST_JSON)\b/g, name => values[name]);
    return 'setImmediate(function () {\nvar runtimeStage = "managed_entry";\ntry {\n' + source
        + '\n} catch(error) { send({runtime_error:String(error), runtime_stage:runtimeStage, '
        + 'runtime_stack:String(error && error.stack || error).slice(0, 8192)}); }\n});';
}

async function bounded(promise, milliseconds, label) {
    let timer;
    try {
        return await Promise.race([promise, new Promise((_, reject) => {
            timer = setTimeout(() => reject(Error(label)), milliseconds);
        })]);
    } finally { clearTimeout(timer); }
}

export async function runWorker(options) {
    let session, script, invocationTimer, invocationStarted = false, invocationSettled = false;
    let stage = 'read_inputs';
    let result = {success:false, status:'runtime_transport_error', operation_started:false};
    const cleanup = {script_unloaded:false, session_detached:false};
    try {
        const rawRequest = readBounded(options.request, 16384);
        const rawProfile = readBounded(options.profile, 16384);
        if (digest(rawRequest) !== options.requestHash || digest(rawProfile) !== options.profileHash)
            throw Error('Request or runtime profile changed.');
        const request = validateRequest(JSON.parse(rawRequest.toString('utf8')));
        if (!started(options.processStart)) throw Error('Expected process start identity missing.');
        if (['rebuild', 'open-existing', 'export'].includes(request.operation) && request.process_started_utc !== options.processStart)
            throw Error('Process identity changed.');
        if (fs.existsSync(request.output)) throw Error('Managed receipt already exists.');
        const profile = JSON.parse(rawProfile.toString('utf8'));
        if (profile.schema_version !== 1) throw Error('Unsupported runtime profile.');
        const node = verifiedFile(profile, 'node');
        if (path.resolve(node).toLowerCase() !== path.resolve(process.execPath).toLowerCase())
            throw Error('Node executable differs from pinned profile.');
        const worker = verifiedFile(profile, 'worker');
        if (path.resolve(worker).toLowerCase() !== path.resolve(process.argv[1]).toLowerCase())
            throw Error('Worker source differs from pinned profile.');
        const modulePath = verifiedFile(profile, 'frida_module');
        const assembly = verifiedFile(profile, 'assembly');
        const template = verifiedFile(profile, 'template');
        const frida = await import(pathToFileURL(modulePath).href);
        const device = await frida.getLocalDevice();
        stage = 'attach_process';
        session = await device.attach(request.process_id);
        const managed = {operation:request.operation, output:request.output, progress_output:request.progress_output,
            timeout_ms:request.timeout_ms,
            process_id:request.process_id, process_started_utc:options.processStart};
        if (['rebuild', 'open-existing', 'export'].includes(request.operation)) {
            managed.solution_id=request.solution_id;
            if (request.operation === 'rebuild') managed.controller_id=request.controller_id;
        }
        if (request.operation === 'list-projects') { managed.offset=request.offset ?? 0; managed.limit=request.limit ?? 100; }
        if (request.operation === 'export') {
            managed.storage_folder=request.storage_folder;
            managed.archive_output=request.archive_output;
        }
        const source = makeSource(readBounded(template).toString('utf8'), assembly, managed);
        stage = 'create_script';
        script = await session.createScript(source);
        let resolveInvocation, rejectInvocation;
        const finished = new Promise((resolve, reject) => { resolveInvocation=resolve; rejectInvocation=reject; });
        // Install before load: the managed entry may complete immediately.
        script.message.connect(message => {
            if (message.type === 'error') {
                const error = Error(message.description || 'Instrumentation failed.');
                error.failureStage = stage;
                rejectInvocation(error);
            }
            if (message.type !== 'send') return;
            const payload = message.payload;
            if (payload?.runtime_error) {
                const error = Error(payload.runtime_error);
                error.failureStage = typeof payload.runtime_stage === 'string' ? payload.runtime_stage : stage;
                if (typeof payload.runtime_stack === 'string') error.stack = payload.runtime_stack.slice(0, 8192);
                rejectInvocation(error);
            }
            if (payload?.probe === 'managed-runtime') {
                invocationSettled = true;
                resolveInvocation(payload);
            }
        });
        invocationStarted = true;
        const invocation = Promise.race([finished, new Promise((_, reject) => {
            invocationTimer=setTimeout(() => reject(Error('Managed invocation outcome unknown.')), request.timeout_ms + 20000);
        })]);
        // Attach a rejection handler immediately even if load itself fails.
        invocation.catch(() => {});
        stage = 'load_script';
        await bounded(script.load(), 10000, 'Instrumentation load outcome unknown.');
        stage = 'await_managed_entry';
        const returned = await invocation;
        if (returned.pid !== request.process_id || returned.hresult !== 0) throw Error('CLR return identity differs.');
        stage = 'read_managed_response';
        const observed = JSON.parse(readBounded(request.output).toString('utf8'));
        if (observed.schema_version !== 1 || observed.process_id !== request.process_id
            || observed.process_started_utc !== options.processStart) throw Error('Managed receipt identity differs.');
        if ((returned.managed_return === 0) !== (observed.success === true)) throw Error('CLR exit contradicts receipt.');
        result = {...observed, managed_return:returned.managed_return};
    } catch (error) {
        result = {success:false, status:'runtime_transport_error', error:String(error),
            failure_stage:typeof error?.failureStage === 'string' ? error.failureStage : stage,
            error_stack:typeof error?.stack === 'string' ? error.stack.slice(0, 8192) : String(error).slice(0, 8192),
            operation_started:invocationStarted, outcome_unknown:invocationStarted};
    } finally {
        clearTimeout(invocationTimer);
        if (script) {
            try { await bounded(script.unload(), 5000, 'Script cleanup unknown.'); cleanup.script_unloaded=true; }
            catch (error) { cleanup.script_error=String(error); }
        } else cleanup.script_unloaded=true;
        if (session) {
            try { await bounded(session.detach(), 5000, 'Session cleanup unknown.'); cleanup.session_detached=true; }
            catch (error) { cleanup.session_error=String(error); }
        } else cleanup.session_detached=true;
    }
    result.transport_cleanup = cleanup;
    result.transport_cleanup_verified = cleanup.script_unloaded && cleanup.session_detached;
    if (!result.transport_cleanup_verified) {
        result.success=false; result.status='runtime_cleanup_unknown';
        result.outcome_unknown=invocationStarted && !invocationSettled;
    }
    return result;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
    const args = process.argv.slice(2);
    if (args.length !== 10 || args[0] !== '--request' || args[2] !== '--profile'
        || args[4] !== '--request-sha256' || args[6] !== '--profile-sha256'
        || args[8] !== '--process-start') {
        console.log(JSON.stringify({success:false,status:'invalid_worker_arguments',operation_started:false}));
        process.exitCode=2;
    } else {
        const result = await runWorker({request:args[1], profile:args[3], requestHash:args[5],
            profileHash:args[7], processStart:args[9]});
        console.log(JSON.stringify(result));
        process.exitCode=result.success ? 0 : 2;
    }
}
