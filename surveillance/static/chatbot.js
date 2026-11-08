"use strict";

const chatForm = document.getElementById("chat-form");
const composer = document.getElementById("chat-compose");
const questionInput = document.getElementById("question");
const conversation = document.getElementById("conversation");

function addMessage(kind, label, message) {
  const entry = document.createElement("div");
  entry.className = `chat-message chat-message-${kind}`;
  const author = document.createElement("strong");
  author.textContent = label;
  const body = document.createElement("p");
  body.textContent = message;
  entry.append(author, body);
  conversation.append(entry);
  conversation.scrollTop = conversation.scrollHeight;
  return body;
}

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (composer.disabled) return;
  const question = questionInput.value.trim();
  if (!question) {
    questionInput.value = "";
    questionInput.reportValidity();
    return;
  }

  const focusWasInComposer = composer.contains(document.activeElement);
  composer.disabled = true;
  document.getElementById("chat-empty").hidden = true;
  addMessage("question", "You", question);
  const reply = addMessage("answer", "Activity chatbot", "Looking up activity…");
  try {
    const result = await demo.json(chatForm.action, {question});
    reply.textContent = result.answer;
    questionInput.value = "";
  } catch (error) {
    reply.textContent = error.message;
    reply.classList.add("error");
  } finally {
    composer.disabled = false;
    conversation.scrollTop = conversation.scrollHeight;
    if (focusWasInComposer && document.activeElement === document.body) questionInput.focus();
  }
});
