// Inline styles to display media rotated 0/90/180/270 degrees (clockwise) inside a
// full-width wrapper, undistorted and snugly fit. `aspect` is the media's natural
// width / height. Rotation is purely a display concern -- the backend records and
// streams in the camera's native sensor orientation, so the viewer rotates here.
//
// For quarter turns the wrapper takes the inverted aspect ratio (a landscape source
// becomes a portrait box) and the media is sized to the wrapper's swapped dimensions
// before rotating, so it fills the box exactly with no black bars and no stretching.
export function rotationStyles(deg, aspect) {
  const a = aspect && aspect > 0 ? aspect : 16 / 9
  const quarter = deg === 90 || deg === 270
  return {
    wrapper: {
      position: 'relative',
      width: '100%',
      aspectRatio: `${quarter ? 1 / a : a}`,
      overflow: 'hidden',
    },
    media: {
      position: 'absolute',
      top: '50%',
      left: '50%',
      width: quarter ? `${a * 100}%` : '100%',
      height: quarter ? `${(1 / a) * 100}%` : '100%',
      objectFit: 'contain',
      transform: `translate(-50%, -50%) rotate(${deg}deg)`,
      transformOrigin: 'center center',
    },
  }
}
