function Pagination({ page, totalPages, setPage }) {
  if (totalPages <= 1) return null
  return (
    <div className="pagination">
      {page > 1
        ? <button onClick={() => setPage(page - 1)}>← Newer</button>
        : <span className="disabled">← Newer</span>}
      <span className="page-info">Page {page} of {totalPages}</span>
      {page < totalPages
        ? <button onClick={() => setPage(page + 1)}>Older →</button>
        : <span className="disabled">Older →</span>}
    </div>
  )
}

export default Pagination;