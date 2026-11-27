const FORMAT_TILE: Record<string, string> = { pdf: 'PDF', docx: 'DOC', markdown: 'MD', txt: 'TXT' }

/** A small tile naming the file type, like the icons in a file manager. */
export function FileTile({ format }: { format: string }) {
  return (
    <span className={`file-tile file-tile--${format}`} aria-hidden="true">
      {FORMAT_TILE[format] ?? format.slice(0, 4).toUpperCase()}
    </span>
  )
}
