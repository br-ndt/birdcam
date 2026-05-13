import { useState, useRef, useEffect } from 'react'

function VideoPlayer({ src, clipName, onDelete, isBatchDeleting, toggleMarkForBatchDelete, markedForBatchDelete, isFavorite, toggleFavorite }) {
    const [isPlaying, setIsPlaying] = useState(false)
    const [progress, setProgress] = useState(0)
    const [currentTime, setCurrentTime] = useState('0:00')
    const [duration, setDuration] = useState('0:00')
    const [isMuted, setIsMuted] = useState(false)
    const [isFullscreen, setIsFullscreen] = useState(false)
    const [showControls, setShowControls] = useState(false)
    const videoRef = useRef(null)
    const controlsTimeoutRef = useRef(null)

    const formatTime = (time) => {
        const minutes = Math.floor(time / 60)
        const seconds = Math.floor(time % 60)
        return `${minutes}:${seconds < 10 ? '0' : ''}${seconds}`
    }

    const togglePlay = () => {
        if (videoRef.current.paused) {
            videoRef.current.play()
            setIsPlaying(true)
        } else {
            videoRef.current.pause()
            setIsPlaying(false)
        }
    }

    const handleTimeUpdate = () => {
        const current = videoRef.current.currentTime
        const total = videoRef.current.duration
        if (!isNaN(total)) {
            setProgress((current / total) * 100)
            setCurrentTime(formatTime(current))
        }
    }

    const handleLoadedMetadata = () => {
        setDuration(formatTime(videoRef.current.duration))
    }

    const handleSeek = (e) => {
        const seekTime = (e.target.value / 100) * videoRef.current.duration
        videoRef.current.currentTime = seekTime
        setProgress(e.target.value)
    }

    const toggleMute = () => {
        videoRef.current.muted = !isMuted
        setIsMuted(!isMuted)
    }

    const toggleFullscreen = () => {
        if (!document.fullscreenElement) {
            videoRef.current.parentElement.requestFullscreen()
            setIsFullscreen(true)
        } else {
            document.exitFullscreen()
            setIsFullscreen(false)
        }
    }

    const handleMouseMove = () => {
        setShowControls(true)
        if (controlsTimeoutRef.current) clearTimeout(controlsTimeoutRef.current)
        controlsTimeoutRef.current = setTimeout(() => {
            if (isPlaying) setShowControls(false)
        }, 2000)
    }

    useEffect(() => {
        return () => {
            if (controlsTimeoutRef.current) clearTimeout(controlsTimeoutRef.current)
        }
    }, [isPlaying])

    return (
        <div
            className="video-container"
            onMouseMove={handleMouseMove}
            onMouseLeave={() => isPlaying && setShowControls(false)}
        >
            <video
                ref={videoRef}
                src={src}
                onClick={togglePlay}
                onTimeUpdate={handleTimeUpdate}
                onLoadedMetadata={handleLoadedMetadata}
                onEnded={() => setIsPlaying(false)}
                preload="metadata"
            />

            <div className={`video-controls ${showControls || !isPlaying ? 'visible' : ''}`}>
                <div className="progress-wrap">
                    <input
                        type="range"
                        className="progress-bar"
                        min="0"
                        max="100"
                        value={progress}
                        onChange={handleSeek}
                        style={{ '--progress': `${progress}%` }}
                    />
                </div>

                <div className="controls-main">
                    <button className="video-btn" onClick={togglePlay}>
                        {isPlaying ? (
                            <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
                                <rect x="6" y="4" width="4" height="16" rx="1" />
                                <rect x="14" y="4" width="4" height="16" rx="1" />
                            </svg>
                        ) : (
                            <svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">
                                <path d="M8 5v14l11-7z" />
                            </svg>
                        )}
                    </button>

                    <div className="time-display">
                        {currentTime} / {duration}
                    </div>

                    <div className="controls-right">
                        <button className={`video-btn favorite-icon ${isFavorite ? 'active' : ''}`} onClick={toggleFavorite} aria-label={isFavorite ? "Unfavorite" : "Favorite"}>
                            <svg viewBox="0 0 24 24" width="20" height="20" fill={isFavorite ? "currentColor" : "none"} stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"></polygon>
                            </svg>
                        </button>
                        {isBatchDeleting ? (
                            <input
                                type="checkbox"
                                className="video-checkbox"
                                checked={markedForBatchDelete}
                                onChange={() => toggleMarkForBatchDelete(clipName)}
                            />
                        ) : (
                            <button className="video-btn delete-icon" onClick={() => onDelete(clipName)} aria-label="Delete">
                                <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                    <path d="M3 6h18"></path>
                                    <path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"></path>
                                    <path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"></path>
                                </svg>
                            </button>
                        )}

                        <button className={`video-btn volume-icon ${isMuted ? 'muted' : ''}`} onClick={toggleMute}>
                            {isMuted ? (
                                <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2">
                                    <path d="M11 5L6 9H2v6h4l5 4V5z" />
                                    <line x1="23" y1="9" x2="17" y2="15" />
                                    <line x1="17" y1="9" x2="23" y2="15" />
                                </svg>
                            ) : (
                                <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2">
                                    <path d="M11 5L6 9H2v6h4l5 4V5z" />
                                    <path d="M19.07 4.93a10 10 0 0 1 0 14.14" />
                                    <path d="M15.54 8.46a5 5 0 0 1 0 7.07" />
                                </svg>
                            )}
                        </button>

                        <button className={`video-btn fullscreen-icon ${isFullscreen ? 'active' : ''}`} onClick={toggleFullscreen}>
                            <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2">
                                <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3" />
                            </svg>
                        </button>
                    </div>
                </div>
            </div>
        </div>
    )
}

export default VideoPlayer
