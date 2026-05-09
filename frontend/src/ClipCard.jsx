function ClipCard({ clip, onDelete, isBatchDeleting, toggleMarkForBatchDelete, markedForBatchDelete }) {
  return (
    <div className="clip">
      <div className="meta">
        <span>{clip.display_time} · {clip.size_mb} MB</span>
        {isBatchDeleting ? (
          <input type="checkbox" checked={markedForBatchDelete} onChange={() => toggleMarkForBatchDelete(clip.name)} />
        ) : (
          <button className="delete-btn" onClick={() => onDelete(clip.name)}>
            Delete
          </button>
        )}
      </div>
      <video controls preload="metadata">
        <source src={`/clips/${clip.name}`} type="video/mp4" />
      </video>
    </div>
  )
}

export default ClipCard