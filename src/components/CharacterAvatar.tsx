import { useState } from 'react';

// Round character avatar. Renders the avatar image when a URL is present,
// falling back to a generated initial chip (the normal path — FF Logs
// provides no avatar URL). Failed image loads fall back
// automatically so a 404 from Lodestone doesn't leave us with broken art.

type Props = {
  name: string;
  avatarUrl?: string;
  size: number;
};

export const CharacterAvatar = ({ name, avatarUrl, size }: Props) => {
  const [failed, setFailed] = useState(false);
  const showImage = avatarUrl && !failed;

  if (showImage) {
    return (
      <img
        className="char-avatar char-avatar-img"
        src={avatarUrl}
        alt=""
        width={size}
        height={size}
        onError={() => setFailed(true)}
        draggable={false}
      />
    );
  }
  return (
    <div
      className="char-avatar char-avatar-fallback"
      style={{ width: size, height: size, fontSize: Math.round(size * 0.42) }}
    >
      {(name || '?').slice(0, 1).toUpperCase()}
    </div>
  );
};
