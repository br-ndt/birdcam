import { useState, useEffect, useCallback } from 'react'
import './App.css'
import logo from './assets/logo.png'
import ClipCard from './ClipCard'
import Pagination from './Pagination'

const POLL_INTERVAL_MS = 10000

export default function App() {
  const [clips, setClips] = useState([])
  const [total, setTotal] = useState(0)
  const [totalPages, setTotalPages] = useState(1)
  const [page, setPage] = useState(() => {
    const params = new URLSearchParams(window.location.search)
    return Math.max(1, parseInt(params.get('page') || '1', 10))
  })
  const [isBatchDeleting, setIsBatchDeleting] = useState(false)
  const [markedForBatchDelete, setMarkedForBatchDelete] = useState(new Set())

  const toggleMarkForBatchDelete = useCallback((name) => {
    setMarkedForBatchDelete(prev => {
      const newSet = new Set(prev)
      if (newSet.has(name)) {
        newSet.delete(name)
      } else {
        newSet.add(name)
      }
      return newSet
    })
  }, [])

  const fetchClips = useCallback(async (pageToFetch) => {
    try {
      const res = await fetch(`/api/clips?page=${pageToFetch}`)
      if (!res.ok) return
      const data = await res.json()
      if (pageToFetch > data.total_pages) {
        setPage(data.total_pages)
        return
      }
      setClips(data.clips)
      setTotal(data.total)
      setTotalPages(data.total_pages)
    } catch (e) {
      console.error('fetch failed', e)
    }
  }, [])

  const handleDelete = useCallback(async (name) => {
    if (isBatchDeleting) {
      setIsBatchDeleting(false)
    } else {
      if (!confirm(`Delete ${name}? This cannot be undone.`)) return
      try {
        const res = await fetch(`/api/clips/${name}`, { method: 'DELETE' })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        fetchClips(page)
      } catch (e) {
        alert(`Failed to delete: ${e.message}`)
      }
    }
  }, [isBatchDeleting, page, fetchClips])

  const confirmBatchDelete = useCallback(() => {
    if (!confirm('Delete all selected clips? This cannot be undone.')) return
    Promise.all(markedForBatchDelete.values().map((name) => fetch(`/api/clips/${name}`, { method: 'DELETE' })))
      .then(() => {
        setIsBatchDeleting(false)
        setMarkedForBatchDelete(new Set())
        fetchClips(page)
      })
      .catch(e => alert(`Failed to delete: ${e.message}`))
  }, [markedForBatchDelete, page, fetchClips])

  useEffect(() => {
    fetchClips(page)
    const url = page === 1 ? '/' : `/?page=${page}`
    window.history.replaceState(null, '', url)
  }, [page, fetchClips])

  useEffect(() => {
    const id = setInterval(() => fetchClips(page), POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [page, fetchClips])

  return (
    <div className="app">
      <section className="logo">
        <img style={{ width: '64px', height: 'auto', borderRadius: '100%' }} src={logo} alt="Logo" />
        <h1>Bird cam</h1>
      </section>
      <section className="live-wrap">
        <h2>Live</h2>
        <img className="live" src="/stream.mjpg" alt="Live feed" />
      </section>
      <section className="clips-section">
      <div className="actions">
        <h2>Clips ({total} total)</h2>
        <Pagination page={page} totalPages={totalPages} setPage={setPage} />
        <div className="batch">
          {isBatchDeleting && (
            <button className="delete-btn" onClick={confirmBatchDelete} disabled={markedForBatchDelete.size === 0}>
              Confirm Batch Delete
            </button>
          )}
          <button className={`delete-btn${isBatchDeleting ? ' active' : ''}`} onClick={() => setIsBatchDeleting(!isBatchDeleting)}>
            {isBatchDeleting ? 'Exit Delete' : 'Batch Delete'}
          </button>
        </div>
      </div>
        <div className="clips">
          {clips.length === 0 && <p className="empty">No clips yet.</p>}
          {clips.map((clip) => (
            <ClipCard key={clip.name} clip={clip} onDelete={handleDelete} isBatchDeleting={isBatchDeleting} toggleMarkForBatchDelete={toggleMarkForBatchDelete} markedForBatchDelete={markedForBatchDelete.has(clip.name)} />
          ))}
        </div>
        <Pagination page={page} totalPages={totalPages} setPage={setPage} />
      </section>
    </div>
  )
}



