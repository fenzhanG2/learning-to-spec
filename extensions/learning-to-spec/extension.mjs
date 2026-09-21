import { joinSession, createCanvas } from '@github/copilot-sdk/extension';
import { startBridge } from './bridge.mjs';
import { createWorkflow } from './workflow.mjs';

let session;
let bridge;
const workflow = createWorkflow({ getSession: () => session, getBridge: () => bridge ||= startBridge(session.sessionId) });
session = await joinSession({ tools: [workflow.tool], commands: [workflow.command], canvases: [createCanvas(workflow.canvas)] });
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => { bridge?.close(); process.exit(0); });
