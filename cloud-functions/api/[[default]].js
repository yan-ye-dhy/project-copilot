import { createProxyHandler } from '../proxy.mjs';

// Configuration and destination are always taken from the server-side context.
// Nothing is enabled or deployed by the presence of this file alone.
const proxy = createProxyHandler();

export default function onRequest(context) {
  return proxy(context);
}
