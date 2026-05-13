import VideoPlayer from './VideoPlayer'

function ClipCard({ clip, onDelete, isBatchDeleting, toggleMarkForBatchDelete, markedForBatchDelete, isFavorite, toggleFavorite }) {
  return (
    <div className="clip">
      <div className="meta">
        <span>{clip.display_time} · {clip.size_mb} MB</span>
      </div>
      <VideoPlayer 
        src={`/clips/${clip.name}`} 
        clipName={clip.name}
        onDelete={onDelete}
        isBatchDeleting={isBatchDeleting}
        toggleMarkForBatchDelete={toggleMarkForBatchDelete}
        markedForBatchDelete={markedForBatchDelete}
        isFavorite={isFavorite}
        toggleFavorite={() => toggleFavorite(clip)}
      />
    </div>
  )
}

export default ClipCard