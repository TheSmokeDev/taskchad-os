import { render } from 'preact';
import { App } from './App';
import './styles/main.css';
import './lib/theme'; // initializes the theme effect on import
import './lib/api';   // initializes dashboardToken from URL/sessionStorage
import { startChatStream } from './lib/chat-stream';

// Open the global chat SSE for the lifetime of the page when a chatId
// is present. Sidebar reads chatUnread from the same signal.
startChatStream();

const root = document.getElementById('app');
if (root) {
  render(<App />, root);
}
