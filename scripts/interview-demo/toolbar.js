// Served only by interview_demo.mjs; excluded from the production build allowlist.
const status = document.getElementById('demo-status');
const request = async (path, body) => {
  const response = await fetch(`/__demo/${path}`, body ? { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Interview-Demo': 'local-only' }, body: JSON.stringify(body) } : {});
  if (!response.ok) throw new Error('模拟服务请求失败');
  return response.json();
};
const state = await request('state');
for (const [name, scenario] of Object.entries(state.scenarios)) {
  const button = document.createElement('button'); button.type = 'button'; button.textContent = scenario.title;
  button.dataset.scenario = name;
  button.addEventListener('click', async () => {
    if (document.getElementById('question-input').disabled) { status.textContent = '请先等待当前模拟任务完成或停止等待。'; return; }
    try {
      await request('scenario', { scenario: name });
      document.querySelector('[data-view="chat"]').click();
      const mode = document.getElementById('chat-mode'); mode.value = scenario.mode; mode.dispatchEvent(new Event('change'));
      const input = document.getElementById('question-input'); input.value = scenario.question; input.dispatchEvent(new Event('input')); input.focus();
      status.textContent = `${scenario.title}已准备，请点击发送。`;
    } catch (error) { status.textContent = error.message; }
  });
  document.getElementById('demo-scenarios').append(button);
}
document.getElementById('demo-reset').addEventListener('click', async () => {
  try {
    await request('reset', {});
    // Only this synthetic account's recovery index is removed, never other browser data.
    localStorage.removeItem('pc.jobs.v1.11111111-1111-4111-8111-111111111111');
    location.reload();
  } catch (error) { status.textContent = error.message; }
});
status.textContent = '选择一个场景；任意输入仍返回该场景的预写结果。';
