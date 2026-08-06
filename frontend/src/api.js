// API client for the Trading OS operational metadata endpoints.
// Holds the bearer token in sessionStorage (clears on browser close — not
// localStorage, so credentials don't linger). Every consumer of the frozen
// /v1/* contract; the UI is just the first client.

const TOKEN_KEY = 'tos_api_key'

export function getToken() {
  return sessionStorage.getItem(TOKEN_KEY)
}

export function setToken(t) {
  sessionStorage.setItem(TOKEN_KEY, t)
}

export function clearToken() {
  sessionStorage.removeItem(TOKEN_KEY)
}

async function get(path, auth = true) {
  const headers = {}
  if (auth) {
    const token = getToken()
    if (!token) throw new Error('no_token')
    headers['Authorization'] = `Bearer ${token}`
  }
  const res = await fetch(path, { headers })
  if (res.status === 401 || res.status === 403) throw new Error('unauthorized')
  if (!res.ok) throw new Error(`http_${res.status}`)
  return res.json()
}

export const api = {
  ping: () => get('/v1/health/ping', false),        // unauthenticated
  summary: () => get('/v1/health/summary'),
  features: () => get('/v1/catalog/features'),
}