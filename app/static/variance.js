const issueForm = document.getElementById('issue-form');
if (issueForm) {
  issueForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = issueForm.querySelector('button');
    const output = document.getElementById('issue-result');
    button.disabled = true;
    output.value = '签发中…';
    try {
      const response = await fetch(issueForm.action, {method: 'POST'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.message);
      output.value = data.reused
        ? `账本已存在该说明，未新增：${data['说明编号']}`
        : `已签发（只追加）：${data['说明编号']}`;
      window.setTimeout(() => window.location.reload(), 900);
    } catch (error) {
      output.dataset.error = '';
      output.value = error.message || '签发失败。';
      button.disabled = false;
    }
  });
}
