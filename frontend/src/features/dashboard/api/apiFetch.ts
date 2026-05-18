// Re-exports from the shared layer. Dashboard code keeps using `./apiFetch`
// so no imports inside this feature need to change.
export { apiFetch, jsonHeaders } from '@/lib/apiFetch';
