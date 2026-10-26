export function MonacoEditor({ value }: { value: string; onChange?: (value: string) => void; language?: string; theme?: string }) { return <pre className="source-view"><code>{value}</code></pre> }
