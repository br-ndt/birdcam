import { useState, useEffect, useCallback } from 'react'
import { withToken } from './api'
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
  const [showFavoritesOnly, setShowFavoritesOnly] = useState(false)

  const [favorites, setFavorites] = useState(() => {
    try {
      const saved = localStorage.getItem('birdcam_favorites')
      return saved ? JSON.parse(saved) : []
    } catch {
      return []
    }
  })

  useEffect(() => {
    localStorage.setItem('birdcam_favorites', JSON.stringify(favorites))
  }, [favorites])

  const toggleFavorite = useCallback((clip) => {
    setFavorites(prev => {
      const exists = prev.some(f => f.name === clip.name)
      if (exists) {
        return prev.filter(f => f.name !== clip.name)
      } else {
        return [...prev, clip]
      }
    })
  }, [])

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
      const res = await fetch(withToken(`/api/clips?page=${pageToFetch}`))
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
        const res = await fetch(withToken(`/api/clips/${name}`), { method: 'DELETE' })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        setFavorites(prev => prev.filter(f => f.name !== name))
        fetchClips(page)
      } catch (e) {
        alert(`Failed to delete: ${e.message}`)
      }
    }
  }, [isBatchDeleting, page, fetchClips])

  const confirmBatchDelete = useCallback(async () => {
    if (!confirm('Delete all selected clips? This cannot be undone.')) return
    try {
      const toDelete = Array.from(markedForBatchDelete.values())
      const res = await fetch(withToken('/api/clips/batch_delete'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ delete: toDelete }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
        setIsBatchDeleting(false)
        setMarkedForBatchDelete(new Set())
        // Clean up deleted items from favorites
      const deletedSet = new Set(toDelete)
      setFavorites(prev => prev.filter(f => !deletedSet.has(f.name)))
        fetchClips(page)
    } catch (e) {
      alert(`Failed to delete: ${e.message}`)
    }
  }, [markedForBatchDelete, page, fetchClips])

  const handleDeleteAllNonFavorited = useCallback(async () => {
    if (!confirm('Delete non-favorited clips on this page? This cannot be undone.')) return
    try {
      console.debug('DeleteNonFav: clips', clips)
      console.debug('DeleteNonFav: favorites', favorites)
      const favSet = new Set(favorites.map(f => f.name))
      const nonFavs = clips.filter(c => !favSet.has(c && c.name))
      console.debug('DeleteNonFav: nonFavs', nonFavs)
      const toDelete = nonFavs.map(c => (c && c.name) || c)
      if (toDelete.length === 0) {
        alert('No non-favorited clips on this page to delete.')
        return
      }
      const res = await fetch(withToken('/api/clips/batch_delete'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ delete: toDelete }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      alert(`Deleted ${data.deleted.length} clips.`)
      const deletedSet = new Set(toDelete)
      setFavorites(prev => prev.filter(f => !deletedSet.has(f.name)))
      fetchClips(page)
    } catch (e) {
      alert(`Failed to delete non-favorited: ${e.message}`)
    }
  }, [favorites, clips, page, fetchClips])

  useEffect(() => {
    fetchClips(page)
    const url = page === 1 ? '/' : `/?page=${page}`
    window.history.replaceState(null, '', url)
  }, [page, fetchClips])

  useEffect(() => {
    const id = setInterval(() => fetchClips(page), POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [page, fetchClips])

  const favNames = new Set(favorites.map(f => f.name))
  const nonFavoritesOnPage = clips.filter(c => !favNames.has(c.name)).length

  return (
    <div className="app">
      <section className="logo">
        <img style={{ width: '64px', height: 'auto', borderRadius: '100%' }} src={logo} alt="Logo" />
        <h1>Bird cam</h1>
      </section>
      <section className="live-wrap">
        <h2>Live</h2>
        <img className="live" src={withToken("/stream.mjpg")} alt="Live feed" />
      </section>
      <section className="clips-section">
        <div className="actions">
          <div className="clips-title">
            <h2>{showFavoritesOnly ? 'Favorites' : 'Clips'}</h2>
            <h3>{showFavoritesOnly ? `${favorites.length} favorites` : `${total} clips`}</h3>
          </div>
          {!showFavoritesOnly && <Pagination page={page} totalPages={totalPages} setPage={setPage} />}
          <div className="btns">
            <button className={`btn favorite-btn${showFavoritesOnly ? ' active' : ''}`} onClick={() => setShowFavoritesOnly(!showFavoritesOnly)}>
              {showFavoritesOnly ? 'Show All' : 'Show Favorites'}
            </button>
            <div className="deletes">
              <button
                className="btn delete-btn"
                onClick={handleDeleteAllNonFavorited}
                disabled={nonFavoritesOnPage === 0}
                title={nonFavoritesOnPage === 0 ? 'No non-favorites on this page' : 'Delete non-favorites on this page'}
              >
                Delete Non-Favorites
              </button>
              {isBatchDeleting && (
                <button className="btn delete-btn" onClick={confirmBatchDelete} disabled={markedForBatchDelete.size === 0}>
                  Confirm Batch Delete
                </button>
              )}
              <button className={`btn${isBatchDeleting ? ' active' : ' delete-btn'}`} onClick={() => setIsBatchDeleting(!isBatchDeleting)}>
                {isBatchDeleting ? 'Exit Delete' : 'Batch Delete'}
              </button>
            </div>
          </div>
        </div>
        <div className="clips">
          {showFavoritesOnly ? (
            favorites.length === 0 ? (
              <p className="empty">No favorites yet.</p>
            ) : (
              favorites.map((clip) => (
                <ClipCard
                  key={clip.name}
                  clip={clip}
                  onDelete={handleDelete}
                  isBatchDeleting={isBatchDeleting}
                  toggleMarkForBatchDelete={toggleMarkForBatchDelete}
                  markedForBatchDelete={markedForBatchDelete.has(clip.name)}
                  isFavorite={true}
                  toggleFavorite={toggleFavorite}
                />
              ))
            )
          ) : (
            clips.length === 0 ? (
              <p className="empty">No clips yet.</p>
            ) : (
              clips.map((clip) => (
                <ClipCard
                  key={clip.name}
                  clip={clip}
                  onDelete={handleDelete}
                  isBatchDeleting={isBatchDeleting}
                  toggleMarkForBatchDelete={toggleMarkForBatchDelete}
                  markedForBatchDelete={markedForBatchDelete.has(clip.name)}
                  isFavorite={favorites.some(f => f.name === clip.name)}
                  toggleFavorite={toggleFavorite}
                />
              ))
            )
          )}
        </div>
        {!showFavoritesOnly && <Pagination page={page} totalPages={totalPages} setPage={setPage} />}
      </section>
    </div>
  )
}



