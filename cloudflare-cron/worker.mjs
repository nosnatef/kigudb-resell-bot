export async function dispatchWorkflow(env, fetcher = fetch) {
  if (!env.GITHUB_TOKEN) throw new Error("Missing GITHUB_TOKEN Worker secret");
  const owner = encodeURIComponent(env.GITHUB_OWNER);
  const repo = encodeURIComponent(env.GITHUB_REPO);
  const workflow = encodeURIComponent(env.GITHUB_WORKFLOW);
  let response;
  try {
    response = await fetcher(
      `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflow}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "kigudb-discord-cron",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ref: env.GITHUB_REF, inputs: { send: true } }),
        signal: AbortSignal.timeout(20000),
      },
    );
  } catch {
    // No automatic retries: GitHub might have accepted the dispatch already.
    throw new Error("GitHub dispatch connection failed; check Actions before retrying");
  }
  if (response.status !== 204) {
    const hint = {
      401: "Check the GITHUB_TOKEN secret and token expiration",
      403: "Check token Actions write permission and repository access",
      404: "Check repository, workflow filename, branch, and token repository access",
      422: "Check workflow_dispatch configuration, branch, and send input",
      429: "GitHub rate limit reached",
    }[response.status] || "Check GitHub service status";
    throw new Error(`GitHub dispatch HTTP ${response.status}: ${hint}`);
  }
  console.log("GitHub accepted the Discord workflow dispatch");
}

export default {
  async scheduled(controller, env) {
    await dispatchWorkflow(env);
  },
};
