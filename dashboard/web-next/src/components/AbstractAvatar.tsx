import Avatar from 'boring-avatars';

// Adapted from OpenBot app/src/components/agents/abstract-avatar.tsx at the
// pinned SHA in upstream-openbot.json.
export function AbstractAvatar({
  name,
  seed,
  size = 40,
}: {
  name: string;
  seed: string;
  size?: number;
}) {
  return (
    <span
      role="img"
      aria-label={name}
      className="avatar"
      style={{ height: size, width: size }}
    >
      <span aria-hidden="true">
        <Avatar name={seed} size={size} />
      </span>
    </span>
  );
}
