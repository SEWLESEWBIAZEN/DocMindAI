import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import '@fontsource-variable/geist/wght.css'
import '@fontsource-variable/geist-mono/wght.css'

import { App } from './app/App'
import './styles/index.css'
import './styles/app.css'

const root = document.getElementById('root')
if (!root) throw new Error('Root element #root not found')

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
