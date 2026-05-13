const TOKEN = import.meta.env.VITE_BIRDCAM_TOKEN;

export const authHeaders = { Authorization: `Bearer ${TOKEN}` };
export const withToken = (path) => `${path}${path.includes("?") ? "&" : "?"}token=${TOKEN}`;